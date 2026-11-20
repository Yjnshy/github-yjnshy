/* 歌单三维封面隧道：穿行、悬停吸起、点击飞入中央并成为主封面。
 *
 * 封面是「有厚度的盒子」的 +Z 面（六面材质中的一面），不再叠一块独立平面。
 * 早先那版用独立平面，悬停倾斜时平面和盒体错开，会露出深色盒面，
 * 看起来像封面被削掉一大块 —— 现在倾斜整个盒子，不可能再错位。
 *
 * 坐标约定：封面边长 S、格距 G、层距 D，第 k 层在 z = -k*D。
 * 相机沿 z 穿行，一层的 z 超过相机就搬到 LAYERS*D 之外，形成无限隧道。
 */

import * as THREE from './three/three.module.min.js';
import { EffectComposer } from './three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from './three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from './three/addons/postprocessing/ShaderPass.js';
import { UnrealBloomPass } from './three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from './three/addons/postprocessing/OutputPass.js';

const S = 2.9;              // 封面边长
const G = 4.4;              // 格间距
const D = 5.5;              // 层间距
const TH = 0.12 * S;        // 盒子厚度
const LAYERS = 14;          // 同屏层数
const CAM_Z0 = 14;          // 相机初始 z：离最近一层 14 个单位
// 相机停在一层前方多远时，那张封面「完整正对镜头」——点封面、以及松手吸附
// 都用这个距离，两处构图一致。吸附如果按 D 的整数倍算会差 1 个单位（画面略近）。
const HERO_DIST = 15;
const COVER_LIMIT = 32;     // 同时上 GPU 的封面数上限
const FLY_MS = 1150;        // 飞入动画时长

const damp = (a, b, k, dt) => a + (b - a) * (1 - Math.exp(-k * dt));
const easeInOut = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

function hash2(i, j) {
  const v = Math.abs(Math.sin(i * 12.9898 + j * 78.233) * 43758.5453);
  return v - Math.floor(v);
}

/* 棋盘格 + 随机稀疏；中央格永远空着，让相机有路可走，
   点某张封面时再把它飞到中央当主封面 */
function slotVisible(i, j, k, h) {
  if ((i + j + k) % 2 !== 0) return false;
  // 外围（中心 3×3 之外）原来还会再随机删掉 62%，于是隧道两侧和远端到处是
  // 黑色空洞。降到 0.28（只删 28%），保留一点参差感，同时把外面一层盒子
  // 基本铺满，画面不再漏出大片底色。
  if (Math.max(Math.abs(i), Math.abs(j)) > 1 && h < 0.28) return false;
  return true;
}

/* 后期调色：轻微色差、从右上光核打出的体积光、暗角、细颗粒 */
const GradeShader = {
  uniforms: {
    tDiffuse: { value: null },
    uTime: { value: 0 },
    uResolution: { value: new THREE.Vector2(1, 1) },
  },
  vertexShader: `varying vec2 vUv;
    void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
  fragmentShader: `varying vec2 vUv;
    uniform sampler2D tDiffuse;
    uniform float uTime;
    uniform vec2 uResolution;
    float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
    void main(){
      vec2 uv = vUv;
      vec2 d = uv - 0.5;
      // 轻微色差：R/B 向相反方向偏一点，G 不动
      float r = texture2D(tDiffuse, uv + d * 0.0015).r;
      float g = texture2D(tDiffuse, uv).g;
      float b = texture2D(tDiffuse, uv - d * 0.0015).b;
      vec3 col = vec3(r, g, b);

      // 体积光：从右上光核朝像素方向 18 次采样，权重每次 ×0.9
      vec2 light = vec2(0.53, 0.47);
      vec2 stepv = (light - uv) / 18.0;
      vec3 rays = vec3(0.0);
      float weight = 1.0;
      vec2 p = uv;
      for (int i = 0; i < 18; i++) {
        p += stepv;
        vec3 s = texture2D(tDiffuse, p).rgb;
        float lum = dot(s, vec3(0.299, 0.587, 0.114));
        rays += s * smoothstep(0.90, 1.0, lum) * weight;
        weight *= 0.9;
      }
      float avoid = smoothstep(0.20, 0.42, length((uv - light) * vec2(1.6, 1.0)));
      col += rays * 0.07 * avoid * vec3(0.92, 0.90, 1.0);

      // 暗角
      float vignette = 1.0 - dot(d * vec2(1.0, 1.25), d * vec2(1.0, 1.25)) * 1.35;
      col *= clamp(vignette, 0.0, 1.0);

      // 颗粒
      float n = hash(uv * uResolution + fract(uTime) * 97.0) - 0.5;
      col += n * 0.011;
      gl_FragColor = vec4(col, 1.0);
    }`,
};


/* 封面材质：倒角 + 细暗边 + 纸纹 + 覆膜扫光 + 边缘淡紫。
   颜色按 sRGB→线性处理（pow 2.2），交给 OutputPass 统一转回。 */
const CoverShader = {
  uniforms: {
    map: { value: null },
    uTime: { value: 0 },
    fogColor: { value: new THREE.Color(0x05060f) },
    fogNear: { value: D * 2.2 },
    fogFar: { value: D * 10.5 },
  },
  vertexShader: `
    attribute vec2 aState;            // x = lift, y = hero
    varying vec2 vUv;
    varying float vFogDepth;
    varying vec2 vState;
    void main(){
      vUv = uv;
      vState = aState;
      vec4 mv = modelViewMatrix * instanceMatrix * vec4(position, 1.0);
      vFogDepth = -mv.z;
      gl_Position = projectionMatrix * mv;
    }`,
  fragmentShader: `
    uniform sampler2D map;
    uniform float uTime;
    uniform vec3 fogColor;
    uniform float fogNear;
    uniform float fogFar;
    varying vec2 vUv;
    varying float vFogDepth;
    varying vec2 vState;
    float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
    void main(){
      // 远处轻微虚化：越远取样半径越大，画面才有景深，不像一面贴图墙
      float dof = smoothstep(14.0, 34.0, vFogDepth);
      vec2 texel = vec2((0.5 + dof * 3.6) / 512.0, (0.5 + dof * 3.6) / 512.0);
      vec3 col = texture2D(map, vUv).rgb * 0.36
               + texture2D(map, vUv + vec2(texel.x, 0.0)).rgb * 0.16
               + texture2D(map, vUv - vec2(texel.x, 0.0)).rgb * 0.16
               + texture2D(map, vUv + vec2(0.0, texel.y)).rgb * 0.16
               + texture2D(map, vUv - vec2(0.0, texel.y)).rgb * 0.16;

      float edge = min(min(vUv.x, 1.0 - vUv.x), min(vUv.y, 1.0 - vUv.y));
      // 最外 .014 是倒角：左上亮、右下暗
      float bevel = smoothstep(0.0, 0.014, edge);
      float corner = (1.0 - vUv.x) * 0.5 + vUv.y * 0.5;
      col *= mix(0.72, 1.0, bevel);
      col += vec3(0.05, 0.05, 0.07) * (1.0 - bevel) * corner;
      // 倒角内侧一条细暗边
      float inner = smoothstep(0.014, 0.020, edge);
      col *= mix(0.93, 1.0, inner);
      // 纸张颗粒
      col += (hash(vUv * 700.0) - 0.5) * 0.009;
      // 覆膜高光：从左上方斜扫，抬起/主封面时更亮
      float sweep = fract(uTime * 0.07 + vUv.y * 0.6) * 2.6 - 0.8;
      float band = exp(-pow((vUv.x * 0.7 + (1.0 - vUv.y) * 0.3) - sweep, 2.0) * 90.0);
      col += band * (0.04 + 0.12 * max(vState.y, vState.x)) * vec3(1.0, 0.95, 1.0);
      // 边缘淡紫 fresnel
      float rim = pow(1.0 - bevel, 6.0);
      col += rim * vec3(0.70, 0.67, 0.94) * 0.07;

      float fogFactor = smoothstep(fogNear, fogFar, vFogDepth);
      col = mix(col, fogColor, fogFactor);
      gl_FragColor = vec4(col, 1.0);
    }`,
};

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
    travelTarget: null,
    snapTarget: null,
    // 滚轮改成「翻页」：累积 deltaY 折算成滚动格数，松手后按格数翻到第 N 张
    // 完整正对镜头的封面，而不是靠惯性滑到哪算哪（那样总是差一点、不好对准）。
    wheelAccum: 0,
    vel: 0,
    lastInput: performance.now(),
    hovered: null,
    heroSlot: null,
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
  renderer.toneMappingExposure = 1.05;
  renderer.domElement.style.display = 'block';
  host.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = bgColor;
  scene.fog = new THREE.Fog(bgColor, D * 2.2, D * 10.5);

  const camera = new THREE.PerspectiveCamera(32, 16 / 9, 0.5, 200);
  camera.position.set(0, 0, CAM_Z0);

  // ---- 后期：RenderPass → 半分辨率 Bloom → 调色 → OutputPass ----
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloomPass = new UnrealBloomPass(new THREE.Vector2(640, 360), 0.18, 0.20, 1.1);
  composer.addPass(bloomPass);
  const gradePass = new ShaderPass(GradeShader);
  composer.addPass(gradePass);
  composer.addPass(new OutputPass());

  // ---- 背景板：隧道尽头的一团光 ----
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

  scene.add(new THREE.AmbientLight(0x5a5680, 1.2));
  const key = new THREE.DirectionalLight(0xffffff, 1.5);
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
  const particleTex = radialTexture();
  const points = new THREE.Points(pGeo, new THREE.PointsMaterial({
    size: 0.09, map: particleTex, transparent: true, opacity: 0.75,
    depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true,
  }));
  scene.add(points);

  // ---- 封面素材：解码完再统一显示，避免一张张弹出来 ----
  const loader = new THREE.TextureLoader();
  let pendingCovers = tracks.length;
  let coversReady = tracks.length === 0;
  const onTexReady = () => {
    pendingCovers -= 1;
    if (pendingCovers <= 0) coversReady = true;
  };
  const textures = tracks.map(track => {
    const tex = loader.load(track.coverUrl, onTexReady, undefined, onTexReady);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 4;
    tex.minFilter = THREE.LinearMipmapLinearFilter;
    tex.magFilter = THREE.LinearFilter;
    return tex;
  });

  // ---- 槽位 ----
  const slots = [];
  for (let layer = 0; layer < LAYERS; layer++) {
    const k = layer;
    for (let j = -5; j <= 5; j++) {
      for (let i = -7; i <= 7; i++) {
        const h = hash2(i + k * 3, j);
        if (!slotVisible(i, j, k, h)) continue;
        // 抖动与旋转都用哈希算，重建场景时位置不变
        const jitter1 = hash2(i * 7.1 + k, j * 3.7);
        const jitter2 = hash2(j * 5.3 - k, i * 2.9);
        const jitter3 = hash2(i * 1.7 + j * 4.1, k * 6.3);
        slots.push({
          i, j, k, layer,
          z: -k * D,
          baseX: i * G + (jitter1 - 0.5) * 2.2,
          baseY: j * G + (jitter2 - 0.5) * 1.8,
          jz: (jitter3 - 0.5) * 1.6,
          rz: (jitter1 - 0.5) * 0.14,
          lift: 0,
          hero: 0,
          hide: 0,
          flying: 0,
          track: Math.abs(3 * i + 5 * j + 2 * k) % count,
        });
      }
    }
  }
  slots.forEach((slot, index) => { slot.index = index; });

  // ---- 几何：一个盒子，封面就是 +Z 面 ----
  // 整片封面绕 Z 倾斜 0.2 弧度：参考图里没有一张是正对相机的
  const coverGroup = new THREE.Group();
  coverGroup.rotation.z = 0.2;
  scene.add(coverGroup);

  const boxGeo = new THREE.BoxGeometry(S, S, TH);
  const sideMat = new THREE.MeshStandardMaterial({ color: 0x252136, roughness: 0.85,
                                                   metalness: 0.05 });
  const backMat = new THREE.MeshStandardMaterial({ color: 0x0c0a16, roughness: 0.9 });

  const byTrack = new Map();
  slots.forEach(slot => {
    if (!byTrack.has(slot.track)) byTrack.set(slot.track, []);
    byTrack.get(slot.track).push(slot);
  });

  const coverMeshes = [];
  byTrack.forEach((list, trackIndex) => {
    const tex = textures[trackIndex % Math.max(1, textures.length)] || null;
    const frontMat = new THREE.ShaderMaterial({
      uniforms: {
        map: { value: tex },
        uTime: { value: 0 },
        fogColor: { value: bgColor },
        fogNear: { value: D * 2.2 },
        fogFar: { value: D * 10.5 },
      },
      vertexShader: CoverShader.vertexShader,
      fragmentShader: CoverShader.fragmentShader,
    });
    // 顺序：+x, -x, +y, -y, +z(封面), -z(背面)
    const materials = [sideMat, sideMat, sideMat, sideMat, frontMat, backMat];
    // 每张封面的 lift/hero 用实例属性传进着色器
    const geo = boxGeo.clone();
    geo.setAttribute('aState', new THREE.InstancedBufferAttribute(
      new Float32Array(list.length * 2), 2));
    const mesh = new THREE.InstancedMesh(geo, materials, list.length);
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.frustumCulled = false;
    list.forEach((slot, index) => {
      slot.meshIdx = index;
      slot.mesh = mesh;
    });
    mesh.userData.slots = list;
    coverGroup.add(mesh);
    coverMeshes.push(mesh);
  });

  const dummy = new THREE.Object3D();
  const pointer = new THREE.Vector2();
  const raycaster = new THREE.Raycaster();
  const clock = new THREE.Clock();

  // ---- 尺寸：16:9 舞台在容器里等比居中 ----
  function layout() {
    // 铺满容器：界面里的舞台是宽扁的，硬套 16:9 会在左右留出两条大黑边。
    // 视野按垂直 32° 固定，宽度方向自然多看到一些，比例不会变形。
    const w = Math.max(240, host.clientWidth || 640);
    const h = Math.max(160, host.clientHeight || 360);
    renderer.setSize(Math.round(w), Math.round(h), true);
    composer.setSize(Math.round(w), Math.round(h));
    if (gradePass.uniforms.uResolution) {
      gradePass.uniforms.uResolution.value.set(Math.round(w), Math.round(h));
    }
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  // ---- 选中：飞到中央 → 成为主封面 → 相机跟到这一层 ----
  function selectSlot(slot, notify) {
    if (!slot) return;
    let center = null;
    let bestDistance = Infinity;
    for (const mate of slots) {
      if (mate.layer !== slot.layer || mate === slot) continue;
      const distance = Math.hypot(mate.baseX, mate.baseY);
      if (distance < bestDistance) { bestDistance = distance; center = mate; }
    }

    const now = performance.now();
    state.flyers.push({
      slot, start: now,
      fromX: slot.baseX, fromY: slot.baseY, toX: 0, toY: 0,
      arc: Math.min(3.2, Math.hypot(slot.baseX, slot.baseY) * 0.32),
      dir: Math.random() < 0.5 ? -1 : 1,
    });
    if (center) {
      state.flyers.push({
        slot: center, start: now,
        fromX: center.baseX, fromY: center.baseY,
        toX: slot.baseX, toY: slot.baseY, arc: 1.2,
        dir: -Math.sign(slot.baseX || 1),
      });
    }

    state.heroSlot = slot;
    state.vel = 0;
    state.travelTarget = CAM_Z0 - (slot.z + HERO_DIST);    // 相机停在这层前方，主封面占六成左右
    state.lastInput = performance.now();

    if (notify) {
      const track = tracks[slot.track % Math.max(1, tracks.length)];
      // hero=false：这是「同一首歌的另一张封面」，页面据此只换主封面、别动播放状态
      if (track) state.onSelect(track.id, { hero: false });
    }
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
      if (t >= 1) {
        fly.slot.baseX = fly.toX;
        fly.slot.baseY = fly.toY;
        fly.slot.flying = 0;
        return false;
      }
      return true;
    });
  }

  function updateSlots(dt) {
    const camZ = camera.position.z;
    const span = LAYERS * D;

    // 1) 往前穿行：越过相机的盒子搬到最远端。一次滚很远时只挪一档会追不上
    //    相机、留下空洞，所以按需要的档数一次挪到位。同一层的 slot.z 恒相等，
    //    spans 也相同，整层会一起回收，层距不会被打乱。
    for (const slot of slots) {
      if (slot.flying) continue;
      if (slot.z > camZ + 1) {
        const spans = Math.ceil((slot.z - camZ - 1) / span);
        slot.z -= spans * span;
        slot.baseX = slot.i * G;
        slot.baseY = slot.j * G;
        if (slot === state.heroSlot) state.heroSlot = null;
        slot.hero = 0;
        slot.lift = 0;
      }
    }

    // 2) 往回缩：相机后退会让整条隧道越退越远，所有封面越过雾深被染成
    //    深色、盒体本身也是深色，于是满屏黑块（就是「缩到一定程度没有新
    //    封面补进来」）。这里按「层」把最远的一层循环搬到最近层前方一层：
    //    每次搬一整层（同层 slot.z 恒相等），层距保持 D、层序不乱，最近一层
    //    始终停在相机前 CAM_Z0 左右，前方一直有清晰封面持续补进来。
    //    不能用「每个盒子各自 floor 取整档」——那样不同盒子档数不同，层序会
    //    错乱，还会把个别盒子搬到贴脸位置，变成一张糊满屏的巨幅封面。
    let guard = LAYERS * 8;
    while (guard-- > 0) {
      let minZ = Infinity, maxZ = -Infinity;
      for (const slot of slots) {
        if (slot.z < minZ) minZ = slot.z;
        if (slot.z > maxZ) maxZ = slot.z;
      }
      if (camZ - maxZ <= CAM_Z0 + D) break;   // 最近层还够近，无需补
      const target = maxZ + D;
      for (const slot of slots) {
        if (Math.abs(slot.z - minZ) > 0.001) continue;
        slot.z = target;
        if (slot.flying) continue;            // 正在飞入的只跟着搬位置，别打断动画
        slot.baseX = slot.i * G;
        slot.baseY = slot.j * G;
        if (slot === state.heroSlot) state.heroSlot = null;
        slot.hero = 0;
        slot.lift = 0;
      }
    }

    // 选中主封面后，比它更靠近相机的那些层会挡在它前面（截图里的问题）。
    // 让它们淡出——相当于给主封面让出视线，而不是把主封面硬推到镜头前。
    // 主封面的实际 z（含抖动与向前抬起的 1.3），比它更靠前的层都要让开
    const heroFrontZ = state.heroSlot
      ? state.heroSlot.z + (state.heroSlot.jz || 0) + 1.3 : null;

    for (const slot of slots) {
      const isHovered = slot === state.hovered;
      const isHero = slot === state.heroSlot;
      // 整组封面绕 Z 倾斜 0.2 弧度，一张 2.9 宽的封面在 z 上要占 ±1 左右，
      // 所以要比主封面「最靠后那一角」再靠前的层让开。
      // 但只让开紧邻的两层：不加这个上限，一次滚很远之后主封面可能落在很远的地方，
      // 判定会把它前面的整条隧道全藏掉，画面就空了。
      const slotZ = slot.z + (slot.jz || 0);
      const wantHide = heroFrontZ !== null && !isHero
        && slotZ > heroFrontZ - 1.2 && slotZ < heroFrontZ + 3 * D;
      slot.hide = state.reduced ? (wantHide ? 1 : 0)
        : damp(slot.hide, wantHide ? 1 : 0, 6, dt);
      slot.lift = state.reduced ? (isHovered ? 1 : 0)
        : damp(slot.lift, isHovered ? 1 : 0, 8, dt);
      slot.hero = state.reduced ? (isHero ? 1 : 0)
        : damp(slot.hero, isHero ? 1 : 0, 2.8, dt);

      const scale = (1 + 0.06 * slot.lift + 0.7 * slot.hero) * (1 - slot.hide);
      const z = slot.z + 0.5 * slot.lift + 1.3 * slot.hero;

      // 倾斜整个盒子：封面是盒子的一面，所以怎么斜都不会和盒体错位
      const tiltX = isHovered && !state.reduced ? pointer.y * 0.18 : 0;
      const tiltY = isHovered && !state.reduced ? -pointer.x * 0.18 : 0;

      dummy.position.set(slot.baseX, slot.baseY, z + slot.jz);
      dummy.rotation.set(tiltX, tiltY, slot.rz);
      dummy.scale.setScalar(scale);
      dummy.updateMatrix();
      slot.mesh.setMatrixAt(slot.meshIdx, dummy.matrix);
      const stateAttr = slot.mesh.geometry.attributes.aState;
      stateAttr.array[slot.meshIdx * 2] = slot.lift;
      stateAttr.array[slot.meshIdx * 2 + 1] = slot.hero;
    }
    coverMeshes.forEach(mesh => {
      mesh.visible = coversReady;
      mesh.instanceMatrix.needsUpdate = true;
      mesh.geometry.attributes.aState.needsUpdate = true;
    });
  }

  let pickTick = 0;
  function pick() {
    if ((pickTick++ % 3) !== 0) return;
    if (Math.abs(state.vel) > 0.5) {   // 穿行时不拾取，否则抬起放下此起彼伏
      state.hovered = null;
      return;
    }
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(coverMeshes, false);
    const hit = hits.find(h => h.instanceId !== undefined);
    if (!hit) { state.hovered = null; return; }
    const slot = hit.object.userData.slots[hit.instanceId];
    state.hovered = (slot && slot.z < camera.position.z - 1) ? slot : null;
  }

  // ---- 输入 ----
  function onWheel(event) {
    event.preventDefault();
    state.travelTarget = null;
    // 新的滚轮要重新决定目标：不清掉旧 snapTarget，吸附进行中的那次滚轮会被
    // 「snapTarget 非空」挡住，结果又拉回上一条（滚几格都原地不动）。
    state.snapTarget = null;
    // 滚轮说明用户想看别的封面：释放主封面，让它平滑缩回网格位置
    // （hero 是 damp 插值，会自己缩回去；baseX/baseY 要手动归位）。
    if (state.heroSlot) {
      const h = state.heroSlot;
      state.heroSlot = null;
      h.baseX = h.i * G;
      h.baseY = h.j * G;
    }
    // 只累积「滚了几格」，翻页交给松手后的吸附来做；vel 给一点点跟手感就行，
    // 系数压得很低，免得滚一下就先冲出去半层。
    state.wheelAccum += event.deltaY;
    state.vel += Math.max(-120, Math.min(120, event.deltaY)) * 0.0012;
    state.lastInput = performance.now();
  }

  let dragging = false, dragStart = 0, dragMoved = 0, lastY = 0;
  function onPointerDown(event) {
    dragging = true;
    dragStart = performance.now();
    dragMoved = 0;
    lastY = event.clientY;
    renderer.domElement.setPointerCapture?.(event.pointerId);
  }
  function onPointerMove(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
    if (dragging) {
      const delta = event.clientY - lastY;
      lastY = event.clientY;
      dragMoved += Math.abs(delta);
      state.travelTarget = null;
      state.vel += delta * 0.02;
      state.lastInput = performance.now();
      state.snapTarget = null;
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
    if (slot === state.heroSlot) {
      // 点的就是中央主封面：不再飞一次，只告诉页面「点的是主封面」
      if (track) state.onSelect(track.id, { hero: true });
      return;
    }
    selectSlot(slot, true);
  }
  function onPointerLeave() { state.hovered = null; }

  // ---- 键盘：方向键在同层挑邻居，空格留给播放器 ----
  const KEY_DIRS = {
    ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
  };
  function isVisible() {
    return host.offsetParent !== null && host.clientWidth > 0;
  }
  function nearestSlot() {
    let best = null, bestDistance = Infinity;
    for (const slot of slots) {
      if (slot.z > camera.position.z - 6 || slot.z < camera.position.z - 30) continue;
      const distance = Math.hypot(slot.baseX, slot.baseY);
      if (distance < bestDistance) { bestDistance = distance; best = slot; }
    }
    return best;
  }
  function nearestInDirection(from, dx, dy) {
    let best = null, bestScore = Infinity;
    for (const slot of slots) {
      if (slot.layer !== from.layer || slot === from) continue;
      const ox = slot.baseX - from.baseX;
      const oy = slot.baseY - from.baseY;
      const along = ox * dx + oy * dy;
      if (along <= 0.1) continue;
      const lateral = Math.abs(ox * dy - oy * dx);
      const score = along + lateral * 1.5;
      if (score < bestScore) { bestScore = score; best = slot; }
    }
    return best;
  }
  function onKeyDown(event) {
    if (!isVisible()) return;
    const tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    const dir = KEY_DIRS[event.key];
    if (!dir) return;
    event.preventDefault();
    const from = state.heroSlot || nearestSlot();
    if (!from) return;
    const next = nearestInDirection(from, dir[0], dir[1]);
    if (next) selectSlot(next, true);
  }

  renderer.domElement.addEventListener('wheel', onWheel, { passive: false });
  renderer.domElement.addEventListener('pointerdown', onPointerDown);
  renderer.domElement.addEventListener('pointermove', onPointerMove);
  renderer.domElement.addEventListener('pointerup', onPointerUp);
  renderer.domElement.addEventListener('pointerleave', onPointerLeave);
  window.addEventListener('keydown', onKeyDown);

  const observer = new ResizeObserver(layout);
  observer.observe(host);
  layout();

  // 一进来就让最近那层的中央封面当主封面：参考图里正中始终是一张大的
  state.heroSlot = slots.find(s => s.i === 0 && s.j === 0 && s.k === 0)
    || slots.find(s => s.layer === 0 && Math.abs(s.baseX) < G && Math.abs(s.baseY) < G)
    || null;

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
      if (state.travelTarget !== null) {
        state.travel = damp(state.travel, state.travelTarget, 2.4, dt);
        if (Math.abs(state.travel - state.travelTarget) < 0.05) {
          state.travel = state.travelTarget;
          state.travelTarget = null;
        }
      } else if (state.snapTarget !== null) {
        // 吸附要软、但得让人感觉得到：4.5 比原来的 3 稍快，松手后约 0.6 秒到位
        state.travel = damp(state.travel, state.snapTarget, 4.5, dt);
        if (Math.abs(state.travel - state.snapTarget) < 0.05) {
          state.travel = state.snapTarget;
          state.snapTarget = null;
        }
      } else {
        state.vel *= Math.pow(0.06, dt);
        if (Math.abs(state.vel) < 0.001) state.vel = 0;
        state.travel += state.vel * dt * 6;
        // 停手就吸附：吸到「某层正好停在相机前 HERO_DIST」的位置，也就是那张
        // 封面完整正对镜头的那一帧。原来吸到 D 的整数倍，差 1 个单位，画面略近、
        // 看着总像没对准；门槛也放宽（320ms / vel<1.1），轻轻一滚松手就能吸住。
        // snapTarget 一旦定下就别每帧重算，否则会被「当前最近层」覆盖，
        // 翻出去的页又被拉回来（之前就是这个 bug，滚几格都停在同一条）。
        if (state.snapTarget === null
            && now - state.lastInput > 300 && Math.abs(state.vel) < 1.2
            && !state.heroSlot) {   // 选了主封面就停在那层，别再吸走
          // 翻页：每 120 的 deltaY 算一格，往前滚 N 格就翻到后面第 N 张，向后同理。
          // 目标是「那一层正好停在相机前 HERO_DIST」，也就是封面完整正对镜头的构图，
          // 跟点封面时的取景一致；一格就能换一张，不用再靠惯性滑过半层。
          const steps = Math.round(state.wheelAccum / 120);
          state.wheelAccum = 0;
          const base = Math.round((state.travel + HERO_DIST - CAM_Z0) / D);
          const k = base + steps;
          state.snapTarget = CAM_Z0 + k * D - HERO_DIST;
        }

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
    gradePass.uniforms.uTime.value = time;
    coverMeshes.forEach(mesh => {
      const mat = mesh.material[4];
      if (mat && mat.uniforms) mat.uniforms.uTime.value = time;
    });
    composer.render();
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
      window.removeEventListener('keydown', onKeyDown);
      textures.forEach(tex => tex.dispose());
      coverMeshes.forEach(mesh => {
        mesh.material.forEach(mat => mat.dispose());
        mesh.geometry.dispose();
      });
      sideMat.dispose();
      backMat.dispose();
      pGeo.dispose();
      particleTex.dispose();
      points.material.dispose();
      bgMesh.geometry.dispose();
      bgMesh.material.dispose();
      composer.dispose();
      bloomPass.dispose();
      gradePass.material.dispose();
      renderer.dispose();
      el.remove();
    },
    setPaused(value) { state.paused = !!value; },
    setTravel(value) { state.travel = value; },
    selectTrack(trackId) {
      // 外部切歌（自动下一首、上一首/下一首按钮）：挑离当前视角最近的一层里，
      // 属于这首歌的封面飞入中央。不能随便取第一张，否则相机会被拽到隧道深处。
      const targetZ = camera.position.z - 15;
      let best = null;
      let bestDistance = Infinity;
      for (const slot of slots) {
        const track = tracks[slot.track % Math.max(1, tracks.length)];
        if (!track || track.id !== trackId) continue;
        const distance = Math.abs(slot.z - targetZ);
        if (distance < bestDistance) { bestDistance = distance; best = slot; }
      }
      if (best) selectSlot(best, false);
    },
    stats() {
      return {
        slots: slots.length,
        meshes: coverMeshes.length,
        tracks: tracks.length,
        camera: camera.position.toArray().map(v => +v.toFixed(2)),
        travel: +state.travel.toFixed(2),
        hovered: !!state.hovered,
        hero: state.heroSlot ? state.heroSlot.index : -1,
        heroTrack: (() => {
          if (!state.heroSlot) return -1;
          const track = tracks[state.heroSlot.track % Math.max(1, tracks.length)];
          return track ? track.id : -1;
        })(),
        loaded: textures.filter(t => !!t.image).length,
        ahead: slots.filter(s => s.z < camera.position.z - 2).length,
        behind: slots.filter(s => s.z > camera.position.z + 1).length,
        // 相机前方、雾深之内（能看清封面）的盒子数。回缩修复靠它做回归断言：
        // 无论怎么缩，这个值都应保持和初始视角同一量级，不能掉到 0 附近。
        near: slots.filter(s => {
          const d = camera.position.z - s.z;
          return d > 2 && d < D * 10.5;
        }).length,
      };
    },
  };
}

window.MusicTunnel = { mount: mountMusicTunnel };
