import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { mergeGeometries } from '/vendor/BufferGeometryUtils.js';
import { VehicleAudio } from '/vehicle-audio.js';

const $ = id => document.getElementById(id);
const UI = { container:$('canvas-container'), viewport:$('viewport'), loading:$('loading'), message:$('loading-message'), error:$('error-banner') };
const DEG = Math.PI / 180;
const COLORS = { teal:0x54e3d3, blue:0x46ddec, return:0xf5ef55, pavement:0x263541, ground:0x0c1720 };
const clockFormat = t => `${String(Math.floor(Math.max(0,t)/60)).padStart(2,'0')}:${(Math.max(0,t)%60).toFixed(1).padStart(4,'0')}`;
const number = v => Number.isFinite(Number(v)) ? Number(v) : 0;
const clamp = (x,a,b) => Math.min(b,Math.max(a,x));
let sceneData, latest=null, previous=null, receivedAt=0, requestBusy=false, lastEventKey=null, selectedTaskId=null, initialized=false;
let renderer, world, camera, controls, center={x:0,y:0}, sceneExtent=1000, cameraMove=null, retryTimer=null, stopped=false;
const rsuObjects=new Map(), signalObjects=new Map(), effects=new Map(), labels=[];
const carParts=[], dummy=new THREE.Object3D(), quaternion=new THREE.Quaternion(), scaleOne=new THREE.Vector3(1,1,1);
const axisY=new THREE.Vector3(0,1,0), projected=new THREE.Vector3();
const layers={};
const QUALITY={light:{fps:24,dpr:.85,pixels:900000,effects:12,pollMs:500,labelMs:250},standard:{fps:30,dpr:1,pixels:1800000,effects:24,pollMs:500,labelMs:180}};
let quality='light',frameTimer=null,frameRequest=0,lastFrameAt=0,labelsDirty=true,lastLabelsAt=0,renderDirty=true,snapshotVersion=0,lastCarVersion=-1,lastCarT=-1,resizeObserver=null;
let oldVehicles=new Map(),cachedCars=[],candidateTasks=[],effectPool=[],stateAbort=null,interpolationMs=450;
let renderedCars=[],carViewDirty=true,carColorKey='',vehicleAudio=null,connectionHealthy=false,audioHold=false,controlBusy=false,controlEpoch=0,adaptiveScale=1,frameCostEMA=0,slowFrames=0,lastAutoQualityAt=0;
const carCullSphere=new THREE.Sphere(new THREE.Vector3(),5);
const carPosition=new THREE.Vector3(),carPalette=[0xf3f4ef,0xc7e0e7,0xf5bd76,0x6b9caa,0x4d7489,0xe6ecec].map(c=>new THREE.Color(c));
const viewFrustum=new THREE.Frustum(),viewMatrix=new THREE.Matrix4(),effectPoint=new THREE.Vector3(),effectSphere=new THREE.Sphere();
const sharedEffects={wave:new THREE.RingGeometry(.84,1,32),packet:new THREE.OctahedronGeometry(2,0)};
const signalBatch={lights:null,count:0,poles:[],cases:[],dirty:false};
const performanceStats={frames:0,since:0,fps:0,totalFrames:0,polls:0,labels:0,effectSkipped:0};
// Following changes only the camera and the selected vehicle's read-only dossier.
const follow={id:null,enabled:false,armed:false,detail:null,error:'',busy:false,epoch:0,lastFetch:null,pinned:false,selectionKey:'',historyKey:'',lastPose:null};
let vehicleAbort=null,followMarker=null,followTrail=null,followLink=null;
const followTrailPoints=[],followPoint=new THREE.Vector3(),followDelta=new THREE.Vector3();
const selectedCarColor=new THREE.Color(0xf4f449),raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2();

function hash(str){let h=2166136261;for(const c of String(str)){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0}
function renderPixelRatio(width,height){const q=QUALITY[quality];return Math.min(window.devicePixelRatio||1,q.dpr,Math.sqrt(q.pixels/Math.max(1,width*height)))*adaptiveScale}
function applyRenderSize(){renderer.setPixelRatio(renderPixelRatio(UI.container.clientWidth,UI.container.clientHeight));renderer.setSize(UI.container.clientWidth,UI.container.clientHeight,false)}
function syncVehicleAudio(){vehicleAudio?.update({vehicle:currentVehicle(),running:connectionHealthy&&!audioHold&&latest?.status==='running',hidden:document.hidden,following:follow.enabled,task:latest&&follow.id?taskForView(latest):null,simTime:latest?.simTime||0})}
function pos(x,y,z=0){return new THREE.Vector3(number(x)-center.x,z,-(number(y)-center.y))}
function material(color,extra={}){const {roughness,metalness,...options}=extra;return new THREE.MeshLambertMaterial({color,...options})}
function box(parent,size,xyz,mat){const o=new THREE.Mesh(new THREE.BoxGeometry(...size),mat);o.position.set(...xyz);o.castShadow=true;o.receiveShadow=true;parent.add(o);return o}
function ring(parent,r,color,opacity=.3,width=1){const m=new THREE.Mesh(new THREE.RingGeometry(Math.max(.01,r-width),r,72),new THREE.MeshBasicMaterial({color,transparent:true,opacity,side:THREE.DoubleSide,depthWrite:false}));m.rotation.x=-Math.PI/2;parent.add(m);return m}
function status(message,kind='connected'){const e=$('connection');e.className='connection '+kind;e.replaceChildren();e.append(document.createElement('i'),document.createTextNode(message))}
function showError(text){UI.error.hidden=!text;UI.error.textContent=text||''}
async function jsonFetch(url,options={}){const controller=new AbortController();if(url.startsWith('/api/state'))stateAbort=controller;if(url.startsWith('/api/vehicle?'))vehicleAbort=controller;const timer=setTimeout(()=>controller.abort(),9000);try{const response=await fetch(url,{cache:'no-store',...options,signal:controller.signal});if(!response.ok){let detail='';try{detail=(await response.json()).error||''}catch{}throw new Error(detail||`HTTP ${response.status}`)}return await response.json()}finally{clearTimeout(timer);if(stateAbort===controller)stateAbort=null;if(vehicleAbort===controller)vehicleAbort=null}}

function setupThree(){
  try{renderer=new THREE.WebGLRenderer({antialias:false,alpha:false,powerPreference:'high-performance'});}catch(error){throw new Error('浏览器无法创建 WebGL 三维画面。请启用硬件加速，或换用支持 WebGL 的 Edge / Chrome。'+error.message)}
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,QUALITY[quality].dpr));renderer.outputColorSpace=THREE.SRGBColorSpace;
  renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.05;renderer.shadowMap.enabled=false;
  UI.container.append(renderer.domElement);
  world=new THREE.Scene();world.background=new THREE.Color(0x0c1720);world.fog=new THREE.Fog(0x0c1720,1900,5500);
  camera=new THREE.PerspectiveCamera(42,1,.8,15000);controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=false;controls.minDistance=32;controls.maxDistance=6500;controls.maxPolarAngle=Math.PI*.465;controls.minPolarAngle=.12;controls.screenSpacePanning=false;controls.target.set(0,0,0);
  controls.addEventListener('start',()=>{cameraMove=null});controls.addEventListener('change',()=>{labelsDirty=true;carViewDirty=true;requestRender()});
  world.add(new THREE.HemisphereLight(0xadc8ec,0x29394a,2.1));const sun=new THREE.DirectionalLight(0xc5e4ed,1.8);sun.position.set(-450,750,350);sun.castShadow=false;world.add(sun);layers.sun=sun;
  for(const name of ['map','rsu','signals','vehicles','effects']){layers[name]=new THREE.Group();world.add(layers[name])}
  const resize=()=>{const w=UI.container.clientWidth,h=UI.container.clientHeight;if(!w||!h)return;applyRenderSize();camera.aspect=w/h;camera.updateProjectionMatrix();if(sceneData&&($('overview-button').classList.contains('active')||$('network-button').classList.contains('active')))overview(false,$('network-button').classList.contains('active'));labelsDirty=true;carViewDirty=true;requestRender()};resizeObserver=new ResizeObserver(resize);resizeObserver.observe(UI.container);resize();
  renderer.domElement.addEventListener('webglcontextlost',event=>{event.preventDefault();stopped=true;connectionHealthy=false;stopFrames();clearTimeout(retryTimer);stateAbort?.abort();vehicleAudio?.update({running:false,following:false});showError('三维图形上下文暂时丢失。请重新加载页面恢复；后端仿真仍可能在运行。')});
}

function addMerged(geometries,mat,parent=layers.map){if(!geometries.length)return null;const geo=mergeGeometries(geometries,false);for(const g of geometries)g.dispose();if(!geo)return null;const mesh=new THREE.Mesh(geo,mat);mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);return mesh}
function mergeStaticNode(group,excluded){const geometries=[],materials=new Set();for(const child of [...group.children]){if(!child.isMesh||excluded.has(child))continue;child.updateMatrix();const g=child.geometry.index?child.geometry.toNonIndexed():child.geometry.clone();g.applyMatrix4(child.matrix);const colors=new Float32Array(g.attributes.position.count*3),c=child.material.color;for(let i=0;i<colors.length;i+=3){colors[i]=c.r;colors[i+1]=c.g;colors[i+2]=c.b}g.setAttribute('color',new THREE.BufferAttribute(colors,3));geometries.push(g);child.geometry.dispose();materials.add(child.material);child.removeFromParent()}addMerged(geometries,material(0xffffff,{vertexColors:true}),group);for(const mat of materials)mat.dispose()}
function stripGeometry(roads,widthExtra=0,height=.1){const p=[];for(const road of roads){const shape=road.shape||[];const half=(number(road.width)||7)/2+widthExtra;for(let i=1;i<shape.length;i++){const a=shape[i-1],b=shape[i],dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy);if(len<.05)continue;const nx=-dy/len*half,ny=dx/len*half;const v=[[a[0]+nx,a[1]+ny],[a[0]-nx,a[1]-ny],[b[0]+nx,b[1]+ny],[b[0]-nx,b[1]-ny]];for(const k of [0,1,2,2,1,3])p.push(v[k][0]-center.x,height,-(v[k][1]-center.y))}}const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(p,3));g.computeVertexNormals();return g}
function polygonShape(points,holes=[]){if(!points||points.length<3)return null;const shape=new THREE.Shape();points.forEach((v,i)=>{if(i===0)shape.moveTo(v[0]-center.x,v[1]-center.y);else shape.lineTo(v[0]-center.x,v[1]-center.y)});shape.closePath();for(const polygon of holes||[]){if(polygon.length<3)continue;const hole=new THREE.Path();polygon.forEach((v,i)=>{if(i===0)hole.moveTo(v[0]-center.x,v[1]-center.y);else hole.lineTo(v[0]-center.x,v[1]-center.y)});hole.closePath();shape.holes.push(hole)}return shape}
function buildMap(data){
  const b=data.bounds||{};center={x:(number(b.minX)+number(b.maxX))/2,y:(number(b.minY)+number(b.maxY))/2};sceneExtent=Math.max(number(b.maxX)-number(b.minX),number(b.maxY)-number(b.minY),500);
  const ground=new THREE.Mesh(new THREE.PlaneGeometry(sceneExtent*3,sceneExtent*3),material(COLORS.ground));ground.rotation.x=-Math.PI/2;ground.position.y=-.5;ground.receiveShadow=true;layers.map.add(ground);
  const areaGroups={water:[],park:[]};for(const area of data.areas||[]){const shape=polygonShape(area.polygon,area.holes);if(!shape)continue;const geo=new THREE.ShapeGeometry(shape);geo.rotateX(-Math.PI/2);geo.translate(0,area.type==='water'?-.15:-.05,0);areaGroups[area.type==='water'?'water':'park'].push(geo)}
  addMerged(areaGroups.water,material(0x103e50,{roughness:.36,metalness:.16}));addMerged(areaGroups.park,material(0x163b37));
  const roads=data.roads||[];const curb=new THREE.Mesh(stripGeometry(roads,1.5,.055),material(0x40515b));curb.receiveShadow=true;layers.map.add(curb);const asphalt=new THREE.Mesh(stripGeometry(roads,0,.12),material(COLORS.pavement));asphalt.receiveShadow=true;layers.map.add(asphalt);
  const linePositions=[];for(const road of roads){if(String(road.id).startsWith(':'))continue;const points=road.shape||[];for(let i=1;i<points.length;i++){const a=points[i-1],b=points[i],dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy);if(len<4)continue;for(let d=3;d<len-1;d+=13){const end=Math.min(d+5,len);linePositions.push(a[0]+dx*d/len-center.x,.2,-(a[1]+dy*d/len-center.y),a[0]+dx*end/len-center.x,.2,-(a[1]+dy*end/len-center.y))}}}
  const lines=new THREE.BufferGeometry();lines.setAttribute('position',new THREE.Float32BufferAttribute(linePositions,3));layers.map.add(new THREE.LineSegments(lines,new THREE.LineBasicMaterial({color:0x81aaa8,transparent:true,opacity:.68})));
  const buildings=[];for(const bld of data.buildings||[]){const shape=polygonShape(bld.polygon,bld.holes);if(!shape)continue;const height=Math.max(2,number(bld.height)||12);const g=new THREE.ExtrudeGeometry(shape,{depth:height,bevelEnabled:false,steps:1});g.rotateX(-Math.PI/2);const color=new THREE.Color([0x314e61,0x29404f,0x476074,0x365969,0x3c465b][hash(bld.id)%5]);const colors=new Float32Array(g.attributes.position.count*3);for(let i=0;i<colors.length;i+=3){colors[i]=color.r;colors[i+1]=color.g;colors[i+2]=color.b}g.setAttribute('color',new THREE.BufferAttribute(colors,3));buildings.push(g)}addMerged(buildings,material(0xffffff,{vertexColors:true,roughness:.82}));
  const named=new Map();for(const road of roads){if(!road.name||String(road.id).startsWith(':'))continue;const old=named.get(road.name);if(!old||(road.shape?.length||0)>(old.shape?.length||0))named.set(road.name,road)}
  for(const road of [...named.values()].slice(0,35)){const point=road.shape[Math.floor(road.shape.length/2)];if(!point)continue;addLabel(road.name,pos(point[0],point[1],5),'road-label','road')}
  for(const rsu of data.rsus||[])createRSU(rsu);for(const intersection of data.intersections||[])createSignal(intersection);
  createCars();createFollowVisuals();$('scene-name').textContent='唐岛湾北岸 · '+(data.rsus||[]).length+' 路口';$('scene-name').title=data.meta?.name||'唐岛湾北岸';$('rsu-count').textContent=`${(data.rsus||[]).length} 个 RSU`;
  const notes=[`地图来源：${data.meta?.source||'OpenStreetMap'}；许可：${data.meta?.license||'ODbL 1.0'}。`,...(data.meta?.notes||[])];const assumed=(data.buildings||[]).filter(v=>!v.heightSource||v.heightSource==='assumed').length;notes.push(`共 ${data.buildings?.length||0} 栋建筑轮廓，其中 ${assumed} 栋采用示意高度。RSU阵列约14m高，车辆、灯组与光效采用便于观察的程序化示意比例；位置遵循仿真坐标，不表示实际设备尺寸或安装形制。灯色按后端受控连接 linkIndex 映射，立杆横向位置为示意。`);$('metadata-notes').textContent=notes.join(' ');
  buildRsuCards();overview(false,true);
}

function addLabel(text,point,className,kind,id=null){const el=document.createElement('div');el.className=className;el.textContent=text;let leader=null;if(kind==='rsu'){leader=document.createElement('div');leader.style.cssText='position:absolute;height:1px;background:#276981aa;transform-origin:0 0;pointer-events:none;display:none';$('map-labels').append(leader)}$('map-labels').append(el);const item={el,point,kind,id,leader};labels.push(item);return el}
function createRSU(data){
  const group=new THREE.Group();group.position.copy(pos(data.x,data.y,.25));layers.rsu.add(group);
  const navy=material(0x274a5c,{metalness:.36}),metal=material(0xc6dcdf,{metalness:.65,roughness:.34}),white=material(0xf0f4ee),blue=material(0x4bc6df,{emissive:0x139eb4,emissiveIntensity:.3});
  box(group,[5.2,.6,4.4],[0,.3,0],material(0xb0bbb9));box(group,[3.6,5.2,2.8],[0,3.2,0],navy);box(group,[3.15,4.55,.1],[0,3.35,1.46],white);for(let i=0;i<5;i++)box(group,[2.45,.11,.12],[0,2.25+i*.35,1.53],navy);const glow=box(group,[2.55,.25,.16],[0,5.17,1.55],blue);
  const pole=new THREE.Mesh(new THREE.CylinderGeometry(.35,.5,13,10),metal);pole.position.set(0,7.5,-.65);pole.castShadow=true;group.add(pole);box(group,[7.5,.3,.45],[0,13.9,-.65],metal);
  for(const x of [-3.4,-1.7,0,1.7,3.4]){const mic=new THREE.Mesh(new THREE.SphereGeometry(.46,8,6),navy);mic.position.set(x,14.1,-.65);group.add(mic)}box(group,[2.0,1.1,1.1],[0,12.45,-.65],white);const halo=ring(group,6.4,COLORS.blue,.8,.65);halo.position.y=.08;
  const radius=number(data.sensingRadiusM)||100;const coverage=new THREE.Group();coverage.position.copy(pos(data.x,data.y,.23));const fill=new THREE.Mesh(new THREE.CircleGeometry(radius,72),new THREE.MeshBasicMaterial({color:0x33b4da,transparent:true,opacity:.055,depthWrite:false,side:THREE.DoubleSide}));fill.rotation.x=-Math.PI/2;coverage.add(fill);ring(coverage,radius,0x179ed0,.26,.9);layers.rsu.add(coverage);
  const label=addLabel('',pos(data.x,data.y,23),'rsu-label','rsu',data.id);const b=document.createElement('b');b.textContent=data.id;const state=document.createElement('span');state.textContent='等待';label.append(b,state);
  mergeStaticNode(group,new Set([glow,halo]));const queueCubes=[];for(let i=0;i<5;i++){const cube=box(group,[1.7,1.25,1.5],[4.2,1+i*1.6,0],material(0x65c5dd,{emissive:0x1596c0,emissiveIntensity:.2}));cube.visible=false;queueCubes.push(cube)}
  rsuObjects.set(data.id,{group,data,coverage,glow,halo,label,queueCubes,point:pos(data.x,data.y,15),busy:false,queue:0});
}
function createSignal(data){
  const group=new THREE.Group();group.position.copy(pos(data.x,data.y,0));layers.signals.add(group);const actionRing=ring(group,12,0x5af1c4,0,1);actionRing.position.y=.32;signalObjects.set(data.id,{group,heads:new Map(),actionRing,mode:'baseline',state:'',changedAt:-100,point:pos(data.x,data.y,6)});
}
function signalHead(signal,link){
  if(!signalBatch.lights){signalBatch.lights=new THREE.InstancedMesh(new THREE.SphereGeometry(.32,6,4),new THREE.MeshBasicMaterial({color:0xffffff}),2048);signalBatch.lights.count=0;signalBatch.lights.frustumCulled=false;layers.signals.add(signalBatch.lights)}
  const head=new THREE.Object3D();head.position.copy(pos(link.x,link.y));head.rotation.y=-number(link.angle)*DEG;head.updateMatrix();const offset=1.3+(number(link.linkIndex)%3)*1.3;
  signalBatch.poles.push(new THREE.BoxGeometry(.3,6,.3).translate(offset,3,0).applyMatrix4(head.matrix));signalBatch.cases.push(new THREE.BoxGeometry(.95,2.65,.7).translate(offset,6.3,0).applyMatrix4(head.matrix));
  const index=signalBatch.count;for(let i=0;i<3;i++){dummy.position.set(offset,7.12-i*.82,.4).applyMatrix4(head.matrix);dummy.quaternion.identity();dummy.scale.setScalar(1);dummy.updateMatrix();signalBatch.lights.setMatrixAt(signalBatch.count++,dummy.matrix)}
  signalBatch.lights.count=signalBatch.count;signalBatch.lights.instanceMatrix.needsUpdate=true;signalBatch.dirty=true;const result={index,activeColor:-1};signal.heads.set(link.linkIndex,result);return result;
}
const signalColors=[new THREE.Color(0xff5760),new THREE.Color(0xffc354),new THREE.Color(0x50e5ad)],signalOff=new THREE.Color(0x263d43);
function colorSignal(head,activeColor){if(head.activeColor===activeColor)return;head.activeColor=activeColor;for(let i=0;i<3;i++)signalBatch.lights.setColorAt(head.index+i,i===activeColor?signalColors[i]:signalOff);signalBatch.lights.instanceColor.needsUpdate=true}
function flushSignalBatch(){if(!signalBatch.dirty)return;addMerged(signalBatch.poles,material(0x617279),layers.signals);addMerged(signalBatch.cases,material(0x20323a),layers.signals);signalBatch.poles=[];signalBatch.cases=[];signalBatch.dirty=false}

function createCars(){
  const definitions=[{g:new THREE.BoxGeometry(1.95,.72,4.5),offset:[0,.88,0],mat:material(0xffffff,{metalness:.2,roughness:.42}),body:true},{g:new THREE.BoxGeometry(1.64,.66,2.5),offset:[0,1.55,.12],mat:material(0xb7d3da,{metalness:.35,roughness:.25})},{g:new THREE.BoxGeometry(1.54,.5,.08),offset:[0,1.59,-1.15],mat:material(0x294953,{metalness:.4,roughness:.2})},{g:new THREE.BoxGeometry(1.54,.45,.08),offset:[0,1.57,1.4],mat:material(0x34545e,{metalness:.4,roughness:.2})},{g:new THREE.BoxGeometry(1.55,.17,.09),offset:[0,.99,-2.28],mat:material(0xfff0c0,{emissive:0xfde5a0,emissiveIntensity:.25})},{g:new THREE.BoxGeometry(1.6,.17,.09),offset:[0,.99,2.28],mat:material(0xb42635,{emissive:0xf13934,emissiveIntensity:.18})}];
  for(const x of [-1.0,1.0])for(const z of [-1.42,1.42]){const g=new THREE.CylinderGeometry(.44,.44,.27,8);g.rotateZ(Math.PI/2);definitions.push({g,offset:[x,.5,z],mat:material(0x253238)})}
  // Bake the fixed offsets into two geometries. One transform per visible car,
  // not ten transforms/uploads for every vehicle in the entire simulation.
  const details=[];
  for(const def of definitions){def.g.translate(...def.offset);if(def.body)continue;
    const g=def.g.index?def.g.toNonIndexed():def.g.clone(),color=def.mat.color,colors=new Float32Array(g.attributes.position.count*3);
    for(let i=0;i<colors.length;i+=3){colors[i]=color.r;colors[i+1]=color.g;colors[i+2]=color.b}
    g.setAttribute('color',new THREE.BufferAttribute(colors,3));details.push(g);def.g.dispose();def.mat.dispose();
  }
  const merged=mergeGeometries(details,false);for(const g of details)g.dispose();
  for(const def of [definitions[0],{g:merged,mat:material(0xffffff,{vertexColors:true}),body:false}]){
    const mesh=new THREE.InstancedMesh(def.g,def.mat,2000);mesh.count=0;mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);mesh.frustumCulled=false;layers.vehicles.add(mesh);carParts.push({...def,mesh});
  }
}
function interpolateAngle(a,b,t){return a+((((b-a)%360)+540)%360-180)*t}
function cacheSnapshot(){
  oldVehicles.clear();for(const v of previous?.vehicles||[])oldVehicles.set(v.id,v);
  cachedCars=(latest.vehicles||[]).slice(0,2000).map(v=>({current:v,previous:oldVehicles.get(v.id)||v}));
  updateCarHighlight();updateCandidates();snapshotVersion++;
  if(follow.armed){const v=chooseDemoVehicle();if(v)selectVehicle(v.id)}
  recordFollowPose();
}
function updateCars(t){
  if(!latest||(lastCarVersion===snapshotVersion&&lastCarT===t&&!carViewDirty))return;
  lastCarVersion=snapshotVersion;lastCarT=t;carViewDirty=false;renderedCars.length=0;
  for(const cached of cachedCars){const v=cached.current,p=cached.previous;
    carPosition.set(THREE.MathUtils.lerp(number(p.x),number(v.x),t)-center.x,0,-(THREE.MathUtils.lerp(number(p.y),number(v.y),t)-center.y));
    carCullSphere.center.copy(carPosition);
    if(v.id!==follow.id&&!viewFrustum.intersectsSphere(carCullSphere))continue;
    quaternion.setFromAxisAngle(axisY,-interpolateAngle(number(p.angle),number(v.angle),t)*DEG);
    dummy.position.copy(carPosition);dummy.quaternion.copy(quaternion);dummy.scale.set(1,1,1);dummy.updateMatrix();
    const i=renderedCars.length;for(const part of carParts)part.mesh.setMatrixAt(i,dummy.matrix);renderedCars.push(cached);
  }
  for(const part of carParts){part.mesh.count=renderedCars.length;part.mesh.instanceMatrix.clearUpdateRanges();part.mesh.instanceMatrix.addUpdateRange(0,renderedCars.length*16);part.mesh.instanceMatrix.needsUpdate=true}
  updateCarHighlight();
}

function effectStages(task,time){let flags=0;if(time>=number(task.created)&&time<number(task.created)+2.5)flags|=1;if(task.origin!==task.target){if(time>=number(task.created)&&time<number(task.txEnd))flags|=2;if(time>=number(task.finish)&&time<number(task.returnEnd))flags|=4}return flags}
function visibleEffectStages(task,flags){if(flags&1){effectPoint.set(number(task.x)-center.x,.45,-(number(task.y)-center.y));effectSphere.set(effectPoint,55);if(!viewFrustum.intersectsSphere(effectSphere))flags&=~1}if(flags&6){const from=rsuObjects.get(task.origin),to=rsuObjects.get(task.target);if(!from||!to)return flags&1;effectPoint.copy(from.point).lerp(to.point,.5);effectPoint.y+=30;effectSphere.set(effectPoint,from.point.distanceTo(to.point)/2+70);if(!viewFrustum.intersectsSphere(effectSphere))flags&=~6}return flags}
function allocateEffect(){const group=new THREE.Group(),wave=new THREE.Mesh(sharedEffects.wave,new THREE.MeshBasicMaterial({color:COLORS.teal,transparent:true,opacity:.6,side:THREE.DoubleSide,depthWrite:false}));wave.rotation.x=-Math.PI/2;const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(25*3),3));const line=new THREE.Line(geometry,new THREE.LineBasicMaterial({color:COLORS.blue,transparent:true,opacity:.6})),packet=new THREE.Mesh(sharedEffects.packet,new THREE.MeshBasicMaterial({color:0x91eaff}));group.add(wave,line,packet);layers.effects.add(group);return{group,wave,line,packet,curve:new THREE.QuadraticBezierCurve3(new THREE.Vector3(),new THREE.Vector3(),new THREE.Vector3()),task:null,stage:0}}
function releaseEffect(effect){effect.group.visible=false;effect.task=null;effect.stage=0;effectPool.push(effect)}
function disposeEffect(effect){effect.group.removeFromParent();effect.line.geometry.dispose();effect.wave.material.dispose();effect.line.material.dispose();effect.packet.material.dispose()}
function trimEffectPool(limit){while(effectPool.length+effects.size>limit&&effectPool.length)disposeEffect(effectPool.pop())}
function configureTransfer(effect,task,stage){if(effect.stage===stage&&effect.task?.id===task.id)return;effect.stage=stage;if(!stage)return;const returning=stage===4,from=rsuObjects.get(returning?task.target:task.origin),to=rsuObjects.get(returning?task.origin:task.target);effect.curve.v0.copy(from.point);effect.curve.v2.copy(to.point);effect.curve.v1.copy(from.point).lerp(to.point,.5);effect.curve.v1.y+=Math.min(60,18+from.point.distanceTo(to.point)*.11);if(returning)effect.curve.v1.x+=8;const buffer=effect.line.geometry.attributes.position;for(let i=0;i<25;i++){effect.curve.getPoint(i/24,effectPoint);buffer.setXYZ(i,effectPoint.x,effectPoint.y,effectPoint.z)}buffer.needsUpdate=true;effect.line.geometry.computeBoundingSphere();effect.line.material.color.setHex(returning?COLORS.return:COLORS.blue);effect.packet.material.color.setHex(returning?0xffd184:0x91eaff)}
function updateEffects(simTime){if(!latest)return;const wanted=new Map(),limit=QUALITY[quality].effects;performanceStats.effectSkipped=0;
  for(let i=candidateTasks.length-1;i>=0;i--){const task=candidateTasks[i],flags=visibleEffectStages(task,effectStages(task,simTime));if(!flags)continue;if(wanted.size>=limit){performanceStats.effectSkipped++;continue}wanted.set(task.id,{task,flags})}
  for(const[id,effect]of effects)if(!wanted.has(id)){effects.delete(id);releaseEffect(effect)}
  for(const[id,{task,flags}]of wanted){let effect=effects.get(id);if(!effect){effect=effectPool.pop()||allocateEffect();effects.set(id,effect)}effect.group.visible=true;effect.wave.visible=!!(flags&1);if(flags&1){const age=simTime-number(task.created),size=3+age*20;effect.wave.position.set(number(task.x)-center.x,.45,-(number(task.y)-center.y));effect.wave.scale.set(size,size,size);effect.wave.material.opacity=.64*(1-age/2.5)}const stage=flags&4?4:flags&2?2:0;configureTransfer(effect,task,stage);effect.task=task;effect.line.visible=effect.packet.visible=!!stage;if(stage){const begin=number(stage===4?task.finish:task.created),end=number(stage===4?task.returnEnd:task.txEnd);effect.curve.getPoint(clamp((simTime-begin)/Math.max(.001,end-begin),0,1),effect.packet.position)}}
  trimEffectPool(limit);
  for(const node of rsuObjects.values()){node.glow.material.emissiveIntensity=node.busy?.9:.14;node.halo.material.opacity=node.busy?.8:.32;for(let i=0;i<node.queueCubes.length;i++)node.queueCubes[i].visible=i<node.queue}
  for(const signal of signalObjects.values()){const age=simTime-signal.changedAt;signal.actionRing.visible=signal.mode==='extended'&&age>=0&&age<4;if(signal.actionRing.visible){signal.actionRing.material.opacity=.65*(1-age/4);signal.actionRing.scale.setScalar(1+age*.3)}}
}

function buildRsuCards(){const list=$('rsu-list');list.replaceChildren();for(const data of sceneData.rsus||[]){const card=document.createElement('button');card.className='rsu-card';card.style.textAlign='left';card.dataset.id=data.id;card.innerHTML='<div class="rsu-card-top"><i class="rsu-dot"></i><b></b><small>等待</small></div><div class="rsu-card-bottom"><span>排队 <strong class="q">0</strong></span><span>完成 <strong class="c">0</strong></span><span>卸载 <strong class="o">0</strong></span></div><div class="queue-bar"><i></i></div>';card.querySelector('b').textContent=data.id;card.addEventListener('click',()=>focusRsu(data.id));list.append(card)}}
function updateDashboard(state){
  const names={running:'运行中',paused:'已暂停',finished:'已完成',error:'异常'};$('run-state').textContent=names[state.status]||state.status||'准备中';$('sim-clock').textContent=clockFormat(number(state.simTime));$('sim-seconds').textContent=number(state.simTime).toFixed(1)+' s';$('play-button').textContent=state.status==='running'?'Ⅱ 暂停':'▶ 继续';$('play-button').disabled=state.status==='error'||state.status==='finished';$('reset-button').disabled=false;
  if(document.activeElement!==$('speed-select'))$('speed-select').value=String(state.speed||1);if(document.activeElement!==$('scheduler-select'))$('scheduler-select').value=state.scheduler||'least_finish';
  const m=state.metrics||{};$('metric-vehicles').textContent=String(m.vehicles??state.vehicles?.length??0);$('metric-completed').replaceChildren(document.createTextNode(String(m.completed??0)+' '));const total=document.createElement('em');total.textContent='/ '+String(m.sensed??0);$('metric-completed').append(total);$('metric-offloaded').textContent=String(m.offloaded??0);$('offload-ratio').textContent=number(m.sensed)>0?`占感知任务 ${(number(m.offloaded)/number(m.sensed)*100).toFixed(0)}%`:'等待任务';$('metric-latency').replaceChildren(document.createTextNode(m.meanLatency==null?'— ':number(m.meanLatency).toFixed(2)+' '));const unit=document.createElement('em');unit.textContent='s';$('metric-latency').append(unit);$('metric-signals').textContent=String(m.signalActions??0);
  const active=(state.tasks||[]).filter(t=>t.status!=='done').length;$('scene-status-text').textContent=`${state.status==='running'?'实时同步':names[state.status]||'同步'} · ${state.vehicles?.length??0} 辆车 · ${active} 项进行中`;
  for(const rsu of state.rsus||[]){const node=rsuObjects.get(rsu.id);if(node){node.busy=!!rsu.busy;node.queue=number(rsu.queue);node.label.classList.toggle('busy',node.busy);node.label.querySelector('span').textContent=node.busy?`计算中 · 排队 ${node.queue}`:`空闲 · 排队 ${node.queue}`;const card=[...$('rsu-list').children].find(c=>c.dataset.id===rsu.id);if(card){card.classList.toggle('busy',node.busy);card.querySelector('small').textContent=node.busy?'● 计算中':'空闲';card.querySelector('.q').textContent=rsu.queue??0;card.querySelector('.c').textContent=rsu.completed??0;card.querySelector('.o').textContent=rsu.offloaded??0;card.querySelector('.queue-bar i').style.width=`${Math.min(100,node.queue*12+(node.busy?8:0))}%`}}}
  for(const data of state.signals||[]){if(!signalObjects.has(data.id))createSignal(data);const signal=signalObjects.get(data.id);const stateText=data.state||'';const links=data.links?.length?data.links:[{x:data.x+9,y:data.y+8,angle:0,linkIndex:0}];const visited=new Set();for(const link of links){if(visited.has(link.linkIndex))continue;visited.add(link.linkIndex);const head=signal.heads.get(link.linkIndex)||signalHead(signal,link);const char=link.state??stateText[link.linkIndex]??'r';const activeColor=/[gG]/.test(char)?2:/[yY]/.test(char)?1:0;colorSignal(head,activeColor)}if(data.mode==='extended'&&signal.mode!=='extended')signal.changedAt=number(state.simTime);signal.mode=data.mode;signal.state=stateText}
  flushSignalBatch();labelsDirty=true;updateFollowPanel();updateEvents(selectedEvents(state));updatePipeline(state);
}
function friendlyEventText(text){let output=String(text||'');for(const junction of sceneData?.intersections||[])if(junction.name)output=output.split(junction.id).join(junction.name);return output}
function updateEvents(events){const signature=(follow.id||'all')+'#'+selectedTaskId+'#'+events.map(e=>e.id).join(',');if(signature===lastEventKey)return;lastEventKey=signature;const list=$('event-list');list.replaceChildren();const names={sense:'感知',dispatch:'调度',complete:'完成',signal:'信号响应',signal_skip:'保持配时',return:'回传',error:'异常'};for(const event of events.slice(-35).reverse()){const item=document.createElement('div');item.className='event-item';item.dataset.type=event.type;item.classList.toggle('selected',event.taskId===selectedTaskId);const time=document.createElement('span');time.className='event-time';time.textContent=number(event.time).toFixed(1);const body=document.createElement('div');body.className='event-body';const tag=document.createElement('span');tag.className='event-type';tag.textContent=names[event.type]||event.type||'事件';body.append(tag,document.createTextNode(friendlyEventText(event.text)));item.append(time,body);if(event.taskId){item.title='查看这项任务的流程';item.addEventListener('click',()=>{selectedTaskId=event.taskId;if(follow.id){follow.pinned=true;updateFollowPanel()}updatePipeline(latest);for(const other of list.children)other.classList.remove('selected');item.classList.add('selected')})}list.append(item)}$('event-count').textContent=(follow.id?'单车 · ':'')+(events.length?`最近 ${Math.min(35,events.length)} 条`:'实时');if(!events.length){const empty=document.createElement('div');empty.className='empty-state';empty.textContent='车辆进入感知区后，事件将在这里出现。';list.append(empty)}}
function updatePipeline(state){
  if(!state)return;
  const task=taskForView(state),steps=[...$('pipeline').querySelectorAll('[data-stage]')];
  const defaults=['生成声学任务','本地 / 跨 RSU','队列与服务时间','完成后才生效','保持安全相位'];
  steps.forEach((el,i)=>{el.classList.remove('done','active');el.querySelector('small').textContent=defaults[i]});
  if(!task){$('trace-label').textContent=follow.id?`${follow.id} · 等待感知触发`:'等待第一项任务';$('pipeline-detail').textContent=follow.id?'车辆进入配置的感知半径且位于受控进口后生成任务。':'锁定一辆车，即可沿它的任务查看整个过程。';return}
  const now=number(follow.id&&follow.detail?follow.detail.simTime:state.simTime),events=selectedEvents(state);
  const signalEvent=events.find(e=>(e.type==='signal'||e.type==='signal_skip')&&e.taskId===task.id);
  const returned=task.status==='done'||task.returned===true;
  const checked=task.controlApplied!=null||!!signalEvent;
  const done=[now>=number(task.created),now>=number(task.txEnd),now>=number(task.finish),returned,checked];
  steps.forEach((el,i)=>el.classList.toggle('done',done[i]));
  const active=!done[1]?1:!done[2]?2:!returned?3:4;steps[active].classList.add('active');
  $('trace-label').textContent=`${task.vehicleId} / ${task.id} · ${task.origin} → ${task.target}`;
  steps[1].querySelector('small').textContent=task.offloaded?`${task.origin} → ${task.target}`:`${task.origin} 本地执行`;
  steps[2].querySelector('small').textContent=task.status==='queued'?`排队 · 约 ${Math.max(0,task.start-now).toFixed(1)} s`:task.status==='processing'?`计算 · 约 ${Math.max(0,task.finish-now).toFixed(1)} s`:done[2]?'计算已结束':'等待任务到达';
  steps[3].querySelector('small').textContent=returned?'已回传源节点':`返回 ${task.origin}`;
  steps[4].querySelector('small').textContent=task.controlApplied===true?`绿灯 +${number(task.extensionS).toFixed(1)} s`:task.controlApplied===false?'条件检查 · 保持配时':checked?'已记录决策':returned?'等待条件检查':'等待结果回传';
  const queue=Math.max(0,task.start-task.txEnd),compute=Math.max(0,task.finish-task.start);
  $('pipeline-detail').textContent=`${number(task.created).toFixed(1)} s 感知 · 预约排队 ${queue.toFixed(1)} s · 配置计算 ${compute.toFixed(1)} s · ${returned?'已于 '+number(task.observedReturnTime??task.returnEnd).toFixed(1):'预计 '+number(task.returnEnd).toFixed(1)} s 回传${task.controlApplied===false?' · '+task.controlReason:''}`;
}

const TASK_NAMES={transmitting:'上传 / 传输',queued:'排队等待',processing:'计算中',returning:'结果回传',done:'处理完成'};
function currentVehicle(){return (latest?.vehicles||[]).find(v=>v.id===follow.id)||null}
function vehicleTasks(){return follow.detail?.tasks||(latest?.tasks||[]).filter(t=>t.vehicleId===follow.id)}
function taskForView(state){
  const tasks=follow.id?vehicleTasks():state.tasks||[];
  let task=tasks.find(t=>t.id===selectedTaskId);
  if(follow.id){
    const now=follow.detail?.simTime??state.simTime;
    if(!task||(!follow.pinned&&task.status==='done'&&now-number(task.controlCheckedTime??task.returnEnd)>4))
      task=tasks.find(t=>t.status!=='done')||tasks.at(-1);
    selectedTaskId=task?.id||null;
    return task;
  }
  return task||tasks.find(t=>t.id===state.trace?.taskId)||[...tasks].reverse().find(t=>t.status!=='done')||tasks.at(-1);
}
function selectedEvents(state){
  if(!follow.id)return state.events||[];
  const ids=new Set(vehicleTasks().map(t=>t.id));
  return follow.detail?.events||(state.events||[]).filter(e=>e.vehicleId===follow.id||ids.has(e.taskId));
}
function updateCarHighlight(){
  const key=follow.id+'#'+renderedCars.map(v=>v.current.id).join('|');if(key===carColorKey)return;carColorKey=key;
  for(const part of carParts)if(part.body){
    for(let i=0;i<renderedCars.length;i++)part.mesh.setColorAt(i,renderedCars[i].current.id===follow.id?selectedCarColor:carPalette[hash(renderedCars[i].current.id)%carPalette.length]);
    if(part.mesh.instanceColor){part.mesh.instanceColor.clearUpdateRanges();part.mesh.instanceColor.addUpdateRange(0,renderedCars.length*3);part.mesh.instanceColor.needsUpdate=true}
  }
}
function updateCandidates(){
  const tasks=new Map((latest?.tasks||[]).map(t=>[t.id,t]));
  // Selected-car effects have first claim on the same bounded effect pool.
  for(const task of follow.detail?.tasks||[])tasks.set(task.id,task);
  const before=number(previous?.simTime??latest?.simTime);
  candidateTasks=[...tasks.values()].filter(t=>number(t.returnEnd)>=before||number(latest?.simTime)-number(t.created)<2.5);
  if(follow.id)candidateTasks.sort((a,b)=>(a.vehicleId===follow.id?1:0)-(b.vehicleId===follow.id?1:0));
}
function clearFollow(){
  vehicleAbort?.abort();follow.epoch++;Object.assign(follow,{id:null,enabled:false,armed:false,detail:null,error:'',busy:false,lastFetch:null,pinned:false,selectionKey:'',historyKey:'',lastPose:null});
  followTrailPoints.length=0;if(followMarker)followMarker.visible=false;if(followTrail)followTrail.visible=false;if(followLink)followLink.visible=false;
  if($('tracking-badge'))$('tracking-badge').hidden=true;
  syncVehicleAudio();
}
function setFollowing(enabled){
  follow.enabled=!!enabled&&!!follow.id;cameraMove=null;
  const v=currentVehicle();
  if(follow.enabled&&v){
    const point=pos(v.x,v.y,1),angle=number(v.angle)*DEG;
    // Elevated chase view; subsequent motion translates the user's orbit/zoom unchanged.
    targetCamera(point,90,false,new THREE.Vector3(-Math.sin(angle)*.6+.28,.95,Math.cos(angle)*.6+.48));
  }
  $('overview-button').classList.remove('active');$('network-button').classList.remove('active');$('focus-button').classList.remove('active');
  updateFollowPanel();requestRender();
}
function selectVehicle(id){
  if(!id)return;
  vehicleAbort?.abort();follow.epoch++;Object.assign(follow,{id,enabled:true,armed:false,detail:null,error:'',busy:false,lastFetch:null,pinned:false,historyKey:'',lastPose:null});
  selectedTaskId=null;lastEventKey=null;followTrailPoints.length=0;
  if(followTrail){followTrail.geometry.setDrawRange(0,0);followTrail.visible=false}
  updateCarHighlight();updateCandidates();setFollowing(true);updatePipeline(latest);refreshVehicleTrace(true);
}
function chooseDemoVehicle(){
  const vehicles=latest?.vehicles||[];
  if(!vehicles.length)return null;
  const activeTasks=new Set((latest.tasks||[]).filter(t=>t.status!=='done').map(t=>t.vehicleId));
  const distance=v=>Math.min(...(sceneData.rsus||[]).map(r=>Math.hypot(v.x-r.x,v.y-r.y)));
  const candidates=vehicles.filter(v=>activeTasks.has(v.id));
  // Prefer an active encounter, then a through vehicle near the demonstration junctions.
  return [...(candidates.length?candidates:vehicles.filter(v=>/^coast_/.test(v.id)).length?vehicles.filter(v=>/^coast_/.test(v.id)):vehicles)].sort((a,b)=>distance(a)-distance(b))[0];
}
async function startFollowDemo(){
  if(!latest)return;
  const v=currentVehicle()||chooseDemoVehicle();if(v)selectVehicle(v.id);else{follow.armed=true;updateFollowPanel()}
  if(latest.status==='paused')await control('resume');
}
function updateVehicleOptions(){
  const select=$('vehicle-select');if(!select||document.activeElement===select)return;
  const vehicles=latest?.vehicles||[],key=vehicles.map(v=>v.id).join('|')+'#'+follow.id;
  if(key===follow.selectionKey)return;follow.selectionKey=key;select.replaceChildren();
  const empty=document.createElement('option');empty.value='';empty.textContent=vehicles.length?'选择车辆 / 或点击地图上的车辆':'暂无车辆 · 点击开始跟车演示';select.append(empty);
  if(follow.id&&!vehicles.some(v=>v.id===follow.id)){const o=document.createElement('option');o.value=follow.id;o.textContent=follow.id+' · 已驶离';select.append(o)}
  for(const v of vehicles){const o=document.createElement('option');o.value=v.id;o.textContent=v.id;select.append(o)}
  select.value=follow.id||'';
}
function roadName(vehicle){
  if(!vehicle)return '—';
  const lane=String(vehicle.laneId||''),edge=lane.replace(/_\d+$/,'');
  return (sceneData?.roads||[]).find(r=>r.id===edge)?.name||(lane.startsWith(':')?'路口内部连接':edge)||'未知道路';
}
function updateFollowPanel(){
  if(!$('vehicle-id'))return;
  syncVehicleAudio();
  updateVehicleOptions();
  const car=currentVehicle(),last=car||follow.detail?.vehicle,task=follow.id&&latest?taskForView(latest):null,tasks=vehicleTasks();
  const departed=!!follow.id&&!car&&follow.detail?.status==='departed';
  const summary=follow.detail?.summary||{sensed:tasks.length,completed:tasks.filter(t=>t.status==='done').length,offloaded:tasks.filter(t=>t.offloaded).length,pending:tasks.filter(t=>t.status!=='done').length};
  $('vehicle-id').textContent=follow.id||'未锁定';
  $('vehicle-speed').textContent=car?(number(car.speed)*3.6).toFixed(1)+' km/h':departed?'已驶离':'—';
  $('vehicle-road').textContent=roadName(last);
  $('vehicle-task-count').textContent=follow.id?`${summary.completed} / ${summary.sensed}`:'—';
  $('follow-status').textContent=follow.error?'档案重连中':follow.armed?'等待第一辆车':departed?'车辆已驶离':follow.id?(follow.enabled?'镜头已锁定':'自由视角 · 档案保留'):'选择一辆车开始';
  $('follow-toggle').disabled=!car||follow.enabled;$('follow-exit').disabled=!follow.enabled;
  $('pick-vehicle-button').disabled=!latest||latest.status==='error'||latest.status==='finished';
  $('tracking-badge').hidden=!follow.id;
  $('tracking-label').textContent=`${follow.id} / ${departed?'已驶离 · 保留任务':follow.enabled?'FOLLOW LOCK':'自由视角'}`;
  $('vehicle-journey').textContent=follow.error?follow.error:!follow.id?(follow.armed?'仿真开始后将锁定一辆车，不自动切换目标。':'选中车辆后，查看它在各 RSU 触发的完整任务链。'):
    `${departed?'车辆已驶离路网；任务继续处理。':!tasks.length?'尚未进入受控进口的感知区。':''}感知 ${summary.sensed} 项 · 卸载 ${summary.offloaded} 项 · 待回传 ${summary.pending} 项。`;
  $('vehicle-signal').textContent=task?(task.controlApplied===true?`信号响应：匹配绿灯延长 ${number(task.extensionS).toFixed(1)} s`:
    task.controlApplied===false?`保持配时：${task.controlReason||'控制条件未满足'}`:`${task.origin} → ${task.target} → ${task.origin} · ${TASK_NAMES[task.status]||'等待任务状态'}，结果回传后检查信号。`):'信号决策将在任务结果回传后显示。';
  const taskSelect=$('vehicle-task-select');
  const signature=tasks.map(t=>`${t.id}:${t.status}:${t.controlApplied}`).join('|')+'#'+selectedTaskId;
  if(signature!==follow.historyKey){
    follow.historyKey=signature;taskSelect.replaceChildren();
    const automatic=document.createElement('option');automatic.value='';automatic.textContent='自动展示当前任务';taskSelect.append(automatic);
    for(const t of tasks){const o=document.createElement('option');o.value=t.id;o.textContent=`${t.id} · ${t.origin} → ${t.target} · ${TASK_NAMES[t.status]||t.status}`;taskSelect.append(o)}
    const list=$('vehicle-task-list');list.replaceChildren();
    for(const t of tasks){const item=document.createElement('button');item.type='button';item.className='vehicle-task-item';item.classList.toggle('selected',t.id===selectedTaskId);item.dataset.task=t.id;
      const heading=document.createElement('b');heading.textContent=`${t.id} / ${t.origin} → ${t.target}`;
      const description=document.createElement('span');description.textContent=`${number(t.created).toFixed(1)} s 感知 · ${TASK_NAMES[t.status]||t.status}${t.controlApplied===true?' · 绿灯 +'+number(t.extensionS).toFixed(1)+' s':t.controlApplied===false?' · 保持配时':''}`;
      item.append(heading,description);item.addEventListener('click',()=>pinVehicleTask(t.id));list.append(item)}
    if(!tasks.length){const empty=document.createElement('p');empty.className='empty-state';empty.textContent='任务会按真实触发顺序保留在这里。';list.append(empty)}
  }
  taskSelect.value=follow.pinned?(selectedTaskId||''):'';taskSelect.disabled=!tasks.length;
}
function pinVehicleTask(id){follow.pinned=!!id;selectedTaskId=id||null;lastEventKey=null;follow.historyKey='';updateFollowPanel();updatePipeline(latest);updateEvents(selectedEvents(latest));requestRender()}
async function refreshVehicleTrace(force=false){
  if(!follow.id||!latest||stopped||document.hidden||(!force&&(follow.busy||follow.lastFetch===latest.simTime)))return;
  const id=follow.id,epoch=follow.epoch,run=latest.runId;follow.busy=true;
  try{
    const data=await jsonFetch('/api/vehicle?id='+encodeURIComponent(id));
    if(epoch!==follow.epoch||id!==follow.id||run!==latest.runId||data.runId!==latest.runId)return;
    follow.detail=data;follow.lastFetch=data.simTime===latest.simTime?latest.simTime:null;follow.error='';
    updateCandidates();updateFollowPanel();updatePipeline(latest);updateEvents(selectedEvents(latest));requestRender();
  }catch(error){if(epoch===follow.epoch&&!document.hidden&&!stopped){follow.error='暂时无法同步单车档案；保留上次记录并自动重试。';updateFollowPanel()}}
  finally{if(epoch===follow.epoch)follow.busy=false}
}
function createFollowVisuals(){
  followMarker=new THREE.Group();followMarker.visible=false;world.add(followMarker);
  ring(followMarker,4.8,0xf4f449,.95,.5).position.y=.3;
  const pointer=new THREE.Mesh(new THREE.ConeGeometry(1.15,2.8,4),new THREE.MeshBasicMaterial({color:0xf4f449,depthTest:false}));pointer.rotation.z=Math.PI;pointer.position.y=7.8;pointer.renderOrder=5;followMarker.add(pointer);
  const lineGeometry=new THREE.BufferGeometry();lineGeometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(120*3),3));lineGeometry.setDrawRange(0,0);
  followTrail=new THREE.Line(lineGeometry,new THREE.LineBasicMaterial({color:0xf4f449,transparent:true,opacity:.7}));followTrail.frustumCulled=false;followTrail.visible=false;world.add(followTrail);
  const linkGeometry=new THREE.BufferGeometry();linkGeometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(3*3),3));
  followLink=new THREE.Line(linkGeometry,new THREE.LineBasicMaterial({color:0x50e6ee,transparent:true,opacity:.5}));followLink.frustumCulled=false;followLink.visible=false;world.add(followLink);
}
function recordFollowPose(){
  const v=currentVehicle();if(!v)return;
  follow.lastPose=v;const last=followTrailPoints.at(-1);
  if(last&&Math.hypot(last.x-v.x,last.y-v.y)<1)return;
  followTrailPoints.push({x:v.x,y:v.y});if(followTrailPoints.length>120)followTrailPoints.shift();
  if(followTrail){const attr=followTrail.geometry.attributes.position;followTrailPoints.forEach((p,i)=>attr.setXYZ(i,p.x-center.x,.35,-(p.y-center.y)));attr.needsUpdate=true;followTrail.geometry.setDrawRange(0,followTrailPoints.length);followTrail.visible=followTrailPoints.length>1}
}
function updateFollowCamera(t){
  const cached=cachedCars.find(v=>v.current.id===follow.id);
  if(followMarker)followMarker.visible=!!cached;
  if(cached){
    const p=cached.previous,v=cached.current;
    followPoint.set(THREE.MathUtils.lerp(number(p.x),number(v.x),t)-center.x,1,-(THREE.MathUtils.lerp(number(p.y),number(v.y),t)-center.y));
    if(followMarker)followMarker.position.copy(followPoint);
    if(follow.enabled){followDelta.copy(followPoint).sub(controls.target);if(followDelta.lengthSq()>.00001){camera.position.add(followDelta);controls.target.copy(followPoint);labelsDirty=true}}
  }
  if(followLink){const task=latest&&follow.id?taskForView(latest):null,from=task&&rsuObjects.get(task.origin),to=task&&rsuObjects.get(task.target);followLink.visible=!!(from&&to&&task.status!=='done');
    if(followLink.visible){const attr=followLink.geometry.attributes.position;attr.setXYZ(0,from.point.x,from.point.y,from.point.z);attr.setXYZ(1,(from.point.x+to.point.x)/2,Math.max(from.point.y,to.point.y)+20,(from.point.z+to.point.z)/2);attr.setXYZ(2,to.point.x,to.point.y,to.point.z);attr.needsUpdate=true}}
}
function setupVehiclePicking(){
  let down=null;
  renderer.domElement.addEventListener('pointerdown',e=>{down=e.button===0?{x:e.clientX,y:e.clientY}:null});
  renderer.domElement.addEventListener('pointerup',e=>{
    if(!down||Math.hypot(e.clientX-down.x,e.clientY-down.y)>5)return;down=null;
    const body=carParts.find(p=>p.body);if(!body||!body.mesh.count)return;
    const rect=renderer.domElement.getBoundingClientRect();pointer.set((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1);
    // Instance matrices move every snapshot; raycasting must use their current bounds.
    body.mesh.computeBoundingSphere();raycaster.setFromCamera(pointer,camera);const hit=raycaster.intersectObject(body.mesh,false)[0];
    if(hit&&renderedCars[hit.instanceId])selectVehicle(renderedCars[hit.instanceId].current.id);
  });
}
function targetCamera(point,distance,animate=true,direction=new THREE.Vector3(.62,.72,.88)){const destination=point.clone().addScaledVector(direction,distance);const viewDistance=destination.distanceTo(point);controls.maxDistance=Math.max(6500,viewDistance*1.2);camera.far=Math.max(15000,viewDistance+sceneExtent*2);camera.updateProjectionMatrix();labelsDirty=true;requestRender();world.fog.near=Math.max(1900,viewDistance*.9);world.fog.far=Math.max(5500,viewDistance+sceneExtent*2);layers.sun.target.position.copy(point);world.add(layers.sun.target);layers.sun.position.copy(point).add(new THREE.Vector3(-450,750,350));if(!animate){cameraMove=null;camera.position.copy(destination);controls.target.copy(point);controls.update();return}cameraMove={from:camera.position.clone(),targetFrom:controls.target.clone(),to:destination,target:point.clone(),start:performance.now()}}
function focusRsu(id){setFollowing(false);const rsu=rsuObjects.get(id);if(!rsu)return;targetCamera(pos(rsu.data.x,rsu.data.y),Math.max(170,number(rsu.data.sensingRadiusM)*1.7));$('focus-button').classList.add('active');$('overview-button').classList.remove('active')}
function focusScene(animate=true){setFollowing(false);const rsus=sceneData.rsus||[];const preferred=rsus[1]||rsus[0];if(preferred){targetCamera(pos(preferred.x??preferred[0],preferred.y??preferred[1]),270,animate)}else if(rsus.length){const first=rsus[Math.floor(rsus.length/2)];targetCamera(pos(first.x,first.y),Math.max(190,number(first.sensingRadiusM)*1.85),animate)}else targetCamera(new THREE.Vector3(0,0,0),Math.min(sceneExtent*.5,700),animate);$('focus-button').classList.add('active');$('overview-button').classList.remove('active')}
function overview(animate=true,regionOnly=false){
  setFollowing(false);const w=UI.container.clientWidth,h=UI.container.clientHeight;
  const fb=sceneData.meta?.focusBounds;const points=regionOnly&&fb?[[fb.minX,fb.minY],[fb.maxX,fb.minY],[fb.minX,fb.maxY],[fb.maxX,fb.maxY]].map(p=>pos(...p)):(sceneData.roads||[]).flatMap(road=>(road.shape||[]).map(p=>pos(p[0],p[1])));if(!points.length)return;
  const point=new THREE.Box3().setFromPoints(points).getCenter(new THREE.Vector3());
  for(const rsu of sceneData.rsus||[])points.push(pos(rsu.x,rsu.y,23));
  const offset=new THREE.Vector3(0,.72,1),back=offset.clone().normalize(),right=new THREE.Vector3().crossVectors(axisY,back).normalize(),up=new THREE.Vector3().crossVectors(back,right);
  const tanV=Math.tan(camera.fov*DEG/2),tanH=tanV*camera.aspect;
  const usableX=Math.max(.12,1-2*Math.min(64,w*.08)/w),usableY=Math.max(.12,1-2*Math.min(150,h*.28)/h);let distance=0;
  // Fit actual road vertices and RSU anchors, looking north; empty box corners and roof heights do not force an unnecessarily distant view.
  for(const p of points){const v=p.clone().sub(point);distance=Math.max(distance,v.dot(back)+Math.abs(v.dot(right))/(tanH*usableX),v.dot(back)+Math.abs(v.dot(up))/(tanV*usableY))}
  targetCamera(point,distance*1.035/offset.length(),animate,offset);$('overview-button').classList.toggle('active',!regionOnly);$('network-button').classList.toggle('active',regionOnly);$('focus-button').classList.remove('active');
}
function drawLabels(){
  const w=UI.container.clientWidth,h=UI.container.clientHeight,compact=$('overview-button').classList.contains('active')||$('network-button').classList.contains('active'),placed=[];
  const ordered=[...labels].sort((a,b)=>(a.kind==='rsu'?-1:1)-(b.kind==='rsu'?-1:1)||(a.kind==='rsu'?String(a.id).localeCompare(String(b.id)):camera.position.distanceToSquared(a.point)-camera.position.distanceToSquared(b.point)));
  const overlaps=r=>placed.some(p=>r.left<p.right+7&&r.right>p.left-7&&r.top<p.bottom+7&&r.bottom>p.top-7);
  for(const item of ordered){
    projected.copy(item.point).project(camera);const anchorX=(projected.x+1)/2*w,anchorY=(-projected.y+1)/2*h;let x=anchorX,y=anchorY;
    let visible=projected.z<1&&projected.z>-1&&x>0&&x<w&&y>0&&y<h,rect;
    if(item.leader)item.leader.style.display='none';
    if(item.kind==='rsu'){
      item.el.style.width=(compact?64:172)+'px';item.el.querySelector('span').style.display=compact?'none':'';item.el.querySelector('b').style.marginRight=compact?'0':'';
      item.el.style.display=visible?'block':'none';
      if(visible){const width=compact?64:172,height=28,left=width/2+18,right=w-width/2-18,top=Math.min(175,h*.34)+height,bottom=h-165;
        const baseX=clamp(x,left,right),baseY=clamp(y-6,top,bottom);let found=false;
        for(const row of [0,-1,1,-2,2,-3,3,-4,4]){for(const column of [0,-1,1]){const cx=baseX+column*(width+10),cy=baseY+row*(height+9);const candidate={left:cx-width/2,right:cx+width/2,top:cy-height,bottom:cy};if(cx>=left&&cx<=right&&cy>=top&&cy<=bottom&&!overlaps(candidate)){x=cx;y=cy;rect=candidate;found=true;break}}if(found)break}
        visible=found;
        if(visible){const endX=clamp(anchorX,rect.left+4,rect.right-4),endY=anchorY<rect.top?rect.top:rect.bottom,dx=endX-anchorX,dy=endY-anchorY;const length=Math.hypot(dx,dy);if(length>5){Object.assign(item.leader.style,{display:'block',left:anchorX+'px',top:anchorY+'px',width:length+'px',transform:`rotate(${Math.atan2(dy,dx)}rad)`})}}
      }
    }else{const width=Math.max(75,item.el.textContent.length*13),height=22;rect={left:x-width/2,right:x+width/2,top:y-height/2,bottom:y+height/2};visible=visible&&x>40&&x<w-40&&y>150&&y<h-155&&camera.position.distanceTo(item.point)<=sceneExtent*1.7&&!overlaps(rect)}
    item.el.style.display=visible?'block':'none';if(visible){item.el.style.left=x+'px';item.el.style.top=y+'px';placed.push(rect)}
  }
}
function stopFrames(){clearTimeout(frameTimer);frameTimer=null;if(frameRequest)cancelAnimationFrame(frameRequest);frameRequest=0}
function requestRender(){renderDirty=true;if(stopped||document.hidden||frameRequest||frameTimer)return;const wait=Math.max(0,1000/QUALITY[quality].fps-(performance.now()-lastFrameAt));frameTimer=setTimeout(()=>{frameTimer=null;if(!document.hidden&&!stopped)frameRequest=requestAnimationFrame(renderFrame)},wait)}
function updateDiagnostics(idle=false){const panel=$('performance-diagnostics');if(!panel||!renderer)return;const now=performance.now(),elapsed=now-performanceStats.since;if(elapsed>=1000){performanceStats.fps=idle?0:performanceStats.frames*1000/elapsed;performanceStats.frames=0;performanceStats.since=now}const info=renderer.info.render;Object.assign(panel.dataset,{quality,fps:(idle?0:performanceStats.fps).toFixed(1),calls:String(info.calls),triangles:String(info.triangles),effects:String(effects.size),pooledEffects:String(effectPool.length),effectObjects:String(effects.size+effectPool.length),effectLimit:String(QUALITY[quality].effects),effectsSkipped:String(performanceStats.effectSkipped),pixelRatio:String(renderer.getPixelRatio()),renderFrames:String(performanceStats.totalFrames),renderedCars:String(renderedCars.length),totalCars:String(cachedCars.length),carDrawBatches:String(carParts.length),frameCpuMs:frameCostEMA.toFixed(2),adaptiveScale:String(adaptiveScale),polls:String(performanceStats.polls),labelUpdates:String(performanceStats.labels),layoutMeasurements:'0',hidden:String(document.hidden),rendering:idle?'idle':'active',shadow:'false',antialias:'false'});$('performance-summary').textContent=(idle?'静止待机':Math.round(performanceStats.fps)+' FPS')+' · '+info.calls+' 次绘制 · '+effects.size+'/'+QUALITY[quality].effects+' 特效';$('performance-details').textContent='画质：'+(quality==='light'?'轻量 24 FPS 上限':'标准 30 FPS 上限')+'；可见车辆 '+renderedCars.length+'/'+cachedCars.length+'，车辆批次 '+carParts.length+'；帧 CPU '+frameCostEMA.toFixed(1)+' ms；自适应比例 '+adaptiveScale.toFixed(2)+'；像素比 '+renderer.getPixelRatio().toFixed(2)+'；三角形 '+info.triangles.toLocaleString()+'；累计绘制 '+performanceStats.totalFrames+'；状态请求 '+performanceStats.polls+'；标签更新 '+performanceStats.labels+'；同步尺寸测量 0；特效池 '+effectPool.length+'，本帧另有 '+performanceStats.effectSkipped+' 项仅显示在事件/指标中。GPU：'+(panel.dataset.renderer||'浏览器未提供')+'。'}
function renderFrame(now){
  frameRequest=0;if(stopped||document.hidden)return;const frameStarted=performance.now();renderDirty=false;lastFrameAt=now;
  const t=latest?.status==='running'?clamp((now-receivedAt)/interpolationMs,0,1):1;
  if(cameraMove){const elapsed=clamp((now-cameraMove.start)/1000,0,1),ease=1-(1-elapsed)**3;camera.position.lerpVectors(cameraMove.from,cameraMove.to,ease);controls.target.lerpVectors(cameraMove.targetFrom,cameraMove.target,ease);labelsDirty=true;if(elapsed===1)cameraMove=null}
  if(latest)updateFollowCamera(t);
  controls.update();camera.updateMatrixWorld();viewMatrix.multiplyMatrices(camera.projectionMatrix,camera.matrixWorldInverse);viewFrustum.setFromProjectionMatrix(viewMatrix);
  let interpolating=false;
  if(latest){updateCars(t);const before=number(previous?.simTime??latest.simTime),time=THREE.MathUtils.lerp(before,number(latest.simTime),t);updateEffects(time);interpolating=t<1&&(cachedCars.length>0||effects.size>0)}
  if(labelsDirty&&now-lastLabelsAt>=QUALITY[quality].labelMs){drawLabels();labelsDirty=false;lastLabelsAt=now;performanceStats.labels++}
  renderer.render(world,camera);vehicleAudio?.renderSignal(now);performanceStats.frames++;performanceStats.totalFrames++;
  const cost=performance.now()-frameStarted;frameCostEMA=frameCostEMA*.9+cost*.1;slowFrames=cost>35?slowFrames+1:Math.max(0,slowFrames-1);
  if(slowFrames>36&&adaptiveScale>.65&&now-lastAutoQualityAt>15000){adaptiveScale=Math.max(.65,adaptiveScale*.8);applyRenderSize();lastAutoQualityAt=now;slowFrames=0;renderDirty=true}
  if(now-performanceStats.since>=1000||!latest||latest.status!=='running')updateDiagnostics(false);
  if(cameraMove||interpolating||renderDirty||labelsDirty)requestRender();else updateDiagnostics(true);
}
function clearDynamic(){clearFollow();renderedCars=[];carColorKey='';carViewDirty=true;for(const effect of effects.values())releaseEffect(effect);effects.clear();trimEffectPool(QUALITY[quality].effects);previous=null;selectedTaskId=null;lastEventKey=null;oldVehicles.clear();cachedCars=[];candidateTasks=[];lastCarVersion=-1;for(const signal of signalObjects.values()){signal.changedAt=-100;signal.mode='baseline'}}
async function poll(){
  if(requestBusy||stopped||document.hidden||controlBusy)return;requestBusy=true;performanceStats.polls++;
  const selected=follow.id,epoch=follow.epoch,operation=controlEpoch;
  try{
    const data=await jsonFetch('/api/state'+(selected?'?vehicle='+encodeURIComponent(selected):''));if(document.hidden||stopped||operation!==controlEpoch)return;connectionHealthy=true;audioHold=false;
    const reset=latest&&(number(data.simTime)<number(latest.simTime)||(data.runId&&latest.runId&&data.runId!==latest.runId));
    const changed=reset||!latest||data.simTime!==latest.simTime||data.status!==latest.status||data.speed!==latest.speed||data.scheduler!==latest.scheduler||data.error!==latest.error;
    if(reset)clearDynamic();
    const trace=data.vehicleTrace;let traceChanged=false;
    if(trace&&epoch===follow.epoch&&selected===follow.id&&trace.vehicleId===selected&&trace.runId===data.runId){traceChanged=follow.lastFetch!==data.simTime||!!follow.error||!follow.detail;follow.detail=trace;follow.lastFetch=data.simTime;follow.error=''}
    if(changed){previous=reset?data:latest;latest=data;const now=performance.now();interpolationMs=clamp(now-receivedAt,200,QUALITY[quality].pollMs);receivedAt=now;cacheSnapshot();updateDashboard(data);requestRender()}
    else if(traceChanged){updateCandidates();updateFollowPanel();updatePipeline(data);updateEvents(selectedEvents(data));requestRender()}
    if(data.status==='error'||data.error){status('仿真异常','error');showError(data.error||'后端仿真停止。请查看本地服务日志，或重置后重试。')}
    else{status(data.status==='paused'?'已连接 · 暂停':'本地仿真已连接');showError('')}
    syncVehicleAudio();UI.loading.hidden=true;initialized=true;updateDiagnostics(!frameRequest&&!frameTimer);
  }catch(error){if(operation===controlEpoch){connectionHealthy=false;if(!document.hidden){vehicleAudio?.update({running:false,hidden:false,following:false});status('连接中断 · 自动重连','error');showError('暂时无法获取仿真状态：'+error.message+'。画面保留最后一次状态，正在自动重连。');if(!initialized)UI.message.textContent='地图已就绪，等待本地仿真服务…'}}}
  finally{requestBusy=false;if(!document.hidden&&!stopped&&!controlBusy)retryTimer=setTimeout(poll,latest?.status==='running'?QUALITY[quality].pollMs:1500)}
}
function setQuality(value){quality=value==='standard'?'standard':'light';adaptiveScale=1;slowFrames=0;applyRenderSize();for(const effect of effects.values())releaseEffect(effect);effects.clear();trimEffectPool(QUALITY[quality].effects);labelsDirty=true;requestRender();updateDiagnostics(false)}
function visibilityChanged(){if(!sceneData||stopped)return;if(document.hidden)connectionHealthy=false;syncVehicleAudio();stopFrames();clearTimeout(retryTimer);if(document.hidden){stateAbort?.abort();vehicleAbort?.abort();updateDiagnostics(true);return}previous=latest;oldVehicles.clear();receivedAt=performance.now();labelsDirty=true;requestRender();poll()}
function disposeScene(){clearFollow();vehicleAudio?.dispose();stopped=true;stopFrames();clearTimeout(retryTimer);stateAbort?.abort();resizeObserver?.disconnect();controls?.dispose();const geometries=new Set(),materials=new Set();world?.traverse(object=>{if(object.geometry)geometries.add(object.geometry);if(object.material)for(const mat of Array.isArray(object.material)?object.material:[object.material])materials.add(mat)});geometries.add(sharedEffects.wave);geometries.add(sharedEffects.packet);for(const g of geometries)g.dispose();for(const m of materials)m.dispose();renderer?.dispose();effects.clear();effectPool=[];cachedCars=[];candidateTasks=[];oldVehicles.clear()}
async function control(action,value){
  if(controlBusy)return;controlBusy=true;controlEpoch++;stateAbort?.abort();
  if(['pause','reset','scheduler'].includes(action))audioHold=true;syncVehicleAudio();
  const buttons=[$('play-button'),$('reset-button'),$('scheduler-select'),$('speed-select')];buttons.forEach(b=>b.disabled=true);$('control-note').textContent='正在应用操作…';
  try{
    const result=await jsonFetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,value})});
    if(result.ok===false)throw new Error(result.error||'后端未能应用指令');
    $('control-note').textContent=result.message||(action==='scheduler'?'已切换策略并重新开始同种子实验。':action==='reset'?'已使用相同随机种子重新开始。':'控制指令已应用。');
    if(action==='reset'||action==='scheduler')clearDynamic();
  }catch(error){connectionHealthy=false;syncVehicleAudio();$('control-note').textContent=`操作未成功：${error.message}`;showError(`操作未成功：${error.message}`)}
  finally{controlBusy=false;buttons.forEach(b=>b.disabled=false);clearTimeout(retryTimer);if(!requestBusy)poll()}
}
function setupUI(){
  vehicleAudio=new VehicleAudio({mount:$('vehicle-audio')});
  $('vehicle-select').addEventListener('change',e=>{if(e.target.value)selectVehicle(e.target.value)});
  $('pick-vehicle-button').addEventListener('click',startFollowDemo);
  $('follow-toggle').addEventListener('click',()=>setFollowing(true));
  $('follow-exit').addEventListener('click',()=>setFollowing(false));
  $('vehicle-task-select').addEventListener('change',e=>pinVehicleTask(e.target.value));
  setupVehiclePicking();
  $('play-button').addEventListener('click',()=>control(latest?.status==='running'?'pause':'resume'));$('reset-button').addEventListener('click',()=>control('reset'));$('speed-select').addEventListener('change',e=>control('speed',Number(e.target.value)));$('scheduler-select').addEventListener('change',e=>control('scheduler',e.target.value));$('overview-button').addEventListener('click',()=>overview());$('network-button').addEventListener('click',()=>overview(true,true));$('focus-button').addEventListener('click',()=>focusScene());$('coverage-toggle').addEventListener('change',e=>{for(const r of rsuObjects.values())r.coverage.visible=e.target.checked;requestRender()});$('quality-select').addEventListener('change',e=>setQuality(e.target.value));document.addEventListener('visibilitychange',visibilityChanged);window.addEventListener('pagehide',disposeScene,{once:true});window.addEventListener('pageshow',event=>{if(event.persisted&&stopped)window.location.reload()});for(const id of ['about-button','data-details'])$(id).addEventListener('click',()=>$('about-dialog').showModal());$('about-dialog').addEventListener('click',e=>{if(e.target===$('about-dialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close()}});
}
async function boot(){try{setupThree();setupUI();sceneData=await jsonFetch('/api/scene');if(!sceneData.roads?.length)throw new Error('地图尚未包含可绘制的道路。请检查地图构建步骤与 /api/scene。');buildMap(sceneData);UI.message.textContent='真实地理场景已载入，正在连接 SUMO…';const gl=renderer.getContext(),extension=gl.getExtension('WEBGL_debug_renderer_info'),panel=$('performance-diagnostics');panel.dataset.vendor=String(extension?gl.getParameter(extension.UNMASKED_VENDOR_WEBGL):gl.getParameter(gl.VENDOR));panel.dataset.renderer=String(extension?gl.getParameter(extension.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER));panel.title=panel.dataset.renderer;performanceStats.since=performance.now();requestRender();poll();}catch(error){status('启动未完成','error');UI.loading.querySelector('.loader').style.display='none';UI.loading.querySelector('h3').textContent='暂时无法载入演示';UI.message.textContent=error.message+' 请确认本地服务已启动后刷新页面。';console.error(error)}}
boot();
