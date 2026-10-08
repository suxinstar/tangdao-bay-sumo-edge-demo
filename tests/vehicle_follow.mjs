import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {pathToFileURL, fileURLToPath} from 'node:url';

// Exercise the shipped app functions, using real Three.js vectors/geometries.
// Browser-only DOM, renderer, frame timers, and HTTP are controlled test doubles.
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const threeUrl=pathToFileURL(path.join(root,'web/vendor/three.module.js')).href;
const THREE=await import(threeUrl);
const utility=fs.readFileSync(path.join(root,'web/vendor/BufferGeometryUtils.js'),'utf8').replace("from 'three'","from '"+threeUrl+"'");
const {mergeGeometries}=await import('data:text/javascript;base64,'+Buffer.from(utility).toString('base64'));
const source=fs.readFileSync(path.join(root,'web/app.js'),'utf8').replace(/^import .*;\r?\n/gm,'').replace(/boot\(\);\s*$/,'');
const sceneFixture=JSON.parse(fs.readFileSync(path.join(root,'data/scene.json'),'utf8'));

function element(){
  const classes=new Set(),queries=new Map(),listeners=new Map();
  return {style:{},dataset:{},children:[],textContent:'',clientWidth:942,clientHeight:625,hidden:false,
    classList:{add(...values){values.forEach(v=>classes.add(v))},remove(...values){values.forEach(v=>classes.delete(v))},contains:v=>classes.has(v),toggle(v,on){on?classes.add(v):classes.delete(v)}},
    append(...values){this.children.push(...values)},replaceChildren(...values){this.children=values},
    querySelector(selector){if(!queries.has(selector))queries.set(selector,element());return queries.get(selector)},
    querySelectorAll(selector){if(!queries.has(selector))queries.set(selector,Array.from({length:5},element));return queries.get(selector)},
    addEventListener(type,handler){listeners.set(type,handler)},
    get offsetWidth(){throw Error('Unexpected synchronous DOM width measurement')},
    get offsetHeight(){throw Error('Unexpected synchronous DOM height measurement')}};
}

function harness(){
  const nodes=new Map(),timers=new Map(),frames=new Map(),requests=[];
  let serial=0;
  const document={hidden:false,activeElement:null,getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)},createElement:element,createTextNode:s=>s,addEventListener(){}};
  const context=vm.createContext({THREE,mergeGeometries,assert,console,document,sceneFixture,
    window:{devicePixelRatio:1,addEventListener(){}},performance:{now:()=>1000},AbortController,
    setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id),
    requestAnimationFrame(fn){const id=++serial;frames.set(id,fn);return id},cancelAnimationFrame:id=>frames.delete(id),
    // Deliberately permit a late response after abort, to test app-level guards.
    fetch(url,options){return new Promise(resolve=>requests.push({url,signal:options.signal,resolve(data){resolve({ok:true,json:async()=>data})}}))}});
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  run(`world=new THREE.Scene();world.fog=new THREE.Fog(0x0c1720,1900,5500);camera=new THREE.PerspectiveCamera(42,942/625,.8,15000);
    controls={target:new THREE.Vector3(),update(){camera.lookAt(this.target);camera.updateMatrixWorld()},dispose(){}};
    renderer={info:{render:{calls:0,triangles:0}},getPixelRatio:()=>QUALITY[quality].dpr,setPixelRatio(){},setSize(){},render(){},dispose(){}};
    for(const name of ['map','rsu','signals','vehicles','effects']){layers[name]=new THREE.Group();world.add(layers[name])}
    layers.sun=new THREE.DirectionalLight();world.add(layers.sun);
    sceneData=sceneFixture;createCars();createFollowVisuals();
    for(const data of sceneData.rsus)createRSU(data);
    latest={runId:'run-1',simTime:30,status:'paused',speed:1,scheduler:'least_finish',vehicles:[{id:'car-A',x:0,y:0,angle:0,speed:2,laneId:'road_0'},{id:'car-B',x:100,y:100,angle:90,speed:3,laneId:'road_0'}],tasks:[],events:[],trace:{taskId:'task-B'},rsus:[],signals:[]};
    previous=latest;cacheSnapshot();
    const taskA={id:'task-A',vehicleId:'car-A',created:10,txEnd:11,start:12,finish:20,returnEnd:21,status:'done',controlCheckedTime:21,controlApplied:false,origin:sceneData.rsus[0].id,target:sceneData.rsus[1].id,x:0,y:0};
    const taskA2={...taskA,id:'task-A2',created:22,txEnd:23,start:25,finish:40,returnEnd:41,status:'processing',controlCheckedTime:null,controlApplied:null};
    const taskB={...taskA2,id:'task-B',vehicleId:'car-B'};
  `);
  return {run,requests,timers,frames,nodes};
}

const checks=[];
async function check(name,body){const h=harness();await body(h);h.run('disposeScene()');checks.push(name)}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const dossier=(id,runId='run-1')=>({vehicleId:id,runId,simTime:30,status:'active',vehicle:{id,x:0,y:0,speed:2},tasks:[],events:[],summary:{sensed:0,completed:0,offloaded:0,pending:0}});

await check('selected_vehicle_tasks_ignore_global_trace',h=>h.run(`
  follow.id='car-A';latest.tasks=[taskA2,taskB];selectedTaskId='task-B';
  assert.equal(taskForView(latest).id,'task-A2');assert.equal(selectedTaskId,'task-A2');
  latest.tasks=[taskB];assert.equal(taskForView(latest),undefined);assert.equal(selectedTaskId,null);
  updatePipeline(latest);assert(document.getElementById('trace-label').textContent.includes('car-A'));
  assert(!document.getElementById('trace-label').textContent.includes('task-B'));
`));

await check('selected_vehicle_events_include_legacy_task_id_without_other_cars',h=>h.run(`
  follow.id='car-A';latest.tasks=[taskA2,taskB];
  latest.events=[{id:'own',vehicleId:'car-A',taskId:'task-A2'},{id:'legacy',taskId:'task-A2'},{id:'other',vehicleId:'car-B',taskId:'task-B'},{id:'global'}];
  assert.equal(JSON.stringify(selectedEvents(latest).map(e=>e.id)),JSON.stringify(['own','legacy']));
`));

await check('archive_retains_selected_tasks_outside_global_window',h=>h.run(`
  follow.id='car-A';latest.tasks=[taskB];latest.events=[{id:'other',vehicleId:'car-B'}];
  follow.detail={simTime:30,tasks:[taskA],events:[{id:'archived-A',taskId:'task-A',vehicleId:'car-A'}]};
  assert.equal(taskForView(latest).id,'task-A');assert.equal(selectedEvents(latest)[0].id,'archived-A');
`));

await check('automatic_task_holds_completed_result_then_advances',h=>h.run(`
  follow.id='car-A';follow.detail={simTime:24,tasks:[taskA,taskA2]};selectedTaskId=taskA.id;
  assert.equal(taskForView(latest).id,'task-A');follow.detail.simTime=26;
  assert.equal(taskForView(latest).id,'task-A2');
`));

await check('manual_pin_survives_later_tasks_and_can_return_to_auto',h=>h.run(`
  follow.id='car-A';follow.detail={simTime:30,tasks:[taskA,taskA2],events:[]};
  pinVehicleTask('task-A');assert.equal(follow.pinned,true);assert.equal(taskForView(latest).id,'task-A');
  follow.detail.simTime=100;assert.equal(taskForView(latest).id,'task-A');
  pinVehicleTask('');assert.equal(follow.pinned,false);assert.equal(taskForView(latest).id,'task-A2');
`));

await check('departed_vehicle_keeps_identity_archive_and_camera',h=>h.run(`
  follow.id='car-A';follow.enabled=true;follow.armed=false;follow.detail={status:'departed',simTime:30,vehicle:latest.vehicles[0],tasks:[taskA2],events:[]};
  previous=latest;latest={...latest,vehicles:[latest.vehicles[1]]};
  camera.position.set(40,50,60);controls.target.set(1,2,3);const cameraBefore=camera.position.clone(),targetBefore=controls.target.clone();
  cacheSnapshot();updateFollowCamera(1);updateFollowPanel();
  assert.equal(follow.id,'car-A');assert.equal(follow.armed,false);assert.equal(taskForView(latest).id,'task-A2');
  assert(camera.position.equals(cameraBefore));assert(controls.target.equals(targetBefore));assert.equal(followMarker.visible,false);
  assert.equal(document.getElementById('vehicle-select').value,'car-A');
  assert(document.getElementById('follow-status').textContent.includes('驶离'));
`));

await check('follow_camera_translates_position_and_target_preserving_orbit_offset',h=>h.run(`
  follow.id='car-A';follow.enabled=true;
  cachedCars=[{previous:{id:'car-A',x:0,y:0,angle:0},current:{id:'car-A',x:20,y:10,angle:180}}];
  camera.position.set(30,41,50);controls.target.set(0,1,0);const offset=camera.position.clone().sub(controls.target);
  updateFollowCamera(.5);assert(controls.target.equals(new THREE.Vector3(10,1,-5)));
  assert(camera.position.clone().sub(controls.target).equals(offset));
  updateFollowCamera(1);assert(controls.target.equals(new THREE.Vector3(20,1,-10)));
  assert(camera.position.clone().sub(controls.target).equals(offset));
`));

await check('paused_follow_stays_still_without_continuous_frame_work',h=>{
  h.run(`follow.id='car-A';follow.enabled=true;updateFollowCamera(1);cameraMove=null;labelsDirty=false;renderDirty=false;stopFrames();
    const pausePosition=camera.position.clone(),pauseTarget=controls.target.clone();
    renderFrame(1000);renderFrame(2000);assert(camera.position.equals(pausePosition));assert(controls.target.equals(pauseTarget));`);
  assert.equal(h.timers.size,0);assert.equal(h.frames.size,0);
});

await check('free_camera_retains_vehicle_dossier_without_chasing',h=>h.run(`
  follow.id='car-A';follow.enabled=true;follow.detail={simTime:30,tasks:[taskA2],events:[]};
  setFollowing(false);const freePosition=camera.position.clone(),freeTarget=controls.target.clone();
  cachedCars=[{previous:latest.vehicles[0],current:{...latest.vehicles[0],x:80,y:40}}];updateFollowCamera(1);
  assert(camera.position.equals(freePosition));assert(controls.target.equals(freeTarget));
  assert.equal(follow.id,'car-A');assert.equal(taskForView(latest).id,'task-A2');
`));

await check('vehicle_trail_is_capped_at_120_in_fixed_buffer',h=>h.run(`
  follow.id='car-A';const buffer=followTrail.geometry.attributes.position.array;
  for(let i=0;i<250;i++){latest.vehicles[0].x=i*2;recordFollowPose()}
  assert.equal(followTrailPoints.length,120);assert.equal(followTrailPoints[0].x,260);assert.equal(followTrailPoints.at(-1).x,498);
  assert.equal(followTrail.geometry.drawRange.count,120);assert.equal(buffer.length,360);
  assert.equal(followTrail.geometry.attributes.position.array,buffer);
  latest.vehicles[0].x+=.5;recordFollowPose();assert.equal(followTrailPoints.at(-1).x,498);
`));

await check('selected_vehicle_effects_prioritized_inside_light_limit_12',h=>h.run(`
  quality='light';follow.id='car-A';latest.simTime=10.5;previous={simTime:10};
  latest.tasks=Array.from({length:60},(_,i)=>({...taskB,id:'other-'+i,created:10,txEnd:11,returnEnd:21}));
  follow.detail={tasks:[{...taskA,id:'selected-effect',created:10,txEnd:11,returnEnd:21}]};
  viewFrustum.intersectsSphere=()=>true;updateCandidates();updateEffects(10.5);
  assert.equal(effects.size,12);assert(effects.has('selected-effect'));assert.equal(performanceStats.effectSkipped,49);
  assert.equal(effects.size+effectPool.length,12);assert.equal(layers.effects.children.length,12);
`));

await check('reset_clears_selected_vehicle_history_and_visuals',h=>h.run(`
  follow.id='car-A';follow.enabled=true;follow.armed=true;follow.pinned=true;follow.historyKey='history';follow.selectionKey='selection';
  follow.detail={simTime:30,tasks:[taskA2],events:[]};selectedTaskId='task-A2';recordFollowPose();
  followMarker.visible=followTrail.visible=followLink.visible=true;const oldEpoch=follow.epoch;
  clearDynamic();assert.equal(follow.id,null);assert.equal(follow.detail,null);assert.equal(follow.enabled,false);assert.equal(follow.armed,false);
  assert.equal(follow.pinned,false);assert.equal(follow.historyKey,'');assert.equal(follow.selectionKey,'');assert.equal(follow.lastPose,null);
  assert.equal(selectedTaskId,null);assert.equal(followTrailPoints.length,0);assert(follow.epoch>oldEpoch);
  assert.equal(followMarker.visible,false);assert.equal(followTrail.visible,false);assert.equal(followLink.visible,false);
`));

await check('late_response_for_previous_selection_cannot_replace_current_dossier',async h=>{
  h.run("selectVehicle('car-A');selectVehicle('car-B')");
  assert.equal(h.requests.length,2);assert.equal(h.requests[0].signal.aborted,true);
  assert.equal(h.requests[0].url,'/api/vehicle?id=car-A');assert.equal(h.requests[1].url,'/api/vehicle?id=car-B');
  h.requests[1].resolve(dossier('car-B'));await flush();
  h.run("assert.equal(follow.detail.vehicleId,'car-B')");
  h.requests[0].resolve(dossier('car-A'));await flush();
  h.run("assert.equal(follow.id,'car-B');assert.equal(follow.detail.vehicleId,'car-B');assert.equal(follow.busy,false)");
});

await check('response_for_previous_run_is_ignored_after_run_change',async h=>{
  const pending=h.run("follow.id='car-A';refreshVehicleTrace(true)");
  h.run("latest.runId='run-2'");h.requests[0].resolve(dossier('car-A','run-1'));await pending;
  h.run("assert.equal(follow.detail,null);assert.equal(follow.busy,false)");
});

await check('server_response_with_wrong_run_id_is_ignored',async h=>{
  const pending=h.run("follow.id='car-A';refreshVehicleTrace(true)");
  h.requests[0].resolve(dossier('car-A','unexpected-run'));await pending;
  h.run("assert.equal(follow.detail,null);assert.equal(follow.busy,false)");
});

await check('late_response_after_reset_cannot_restore_old_selection',async h=>{
  h.run("selectVehicle('car-A');clearDynamic()");
  assert.equal(h.requests[0].signal.aborted,true);h.requests[0].resolve(dossier('car-A'));await flush();
  h.run("assert.equal(follow.id,null);assert.equal(follow.detail,null);assert.equal(selectedTaskId,null);assert.equal(followTrailPoints.length,0)");
});

await check('hidden_page_and_unchanged_sim_time_skip_vehicle_fetch',async h=>{
  await h.run("follow.id='car-A';document.hidden=true;refreshVehicleTrace(true)");assert.equal(h.requests.length,0);
  await h.run("document.hidden=false;follow.lastFetch=latest.simTime;refreshVehicleTrace()");assert.equal(h.requests.length,0);
});

await check('vehicle_geometry_is_two_batches_with_one_transform_each',h=>h.run(`
  assert.equal(carParts.length,2);viewFrustum.intersectsSphere=()=>true;updateCars(.5);
  assert.equal(renderedCars.length,2);assert.equal(carParts[0].mesh.count,2);assert.equal(carParts[1].mesh.count,2);
  const a=new THREE.Matrix4(),b=new THREE.Matrix4();carParts[0].mesh.getMatrixAt(1,a);carParts[1].mesh.getMatrixAt(1,b);assert(a.equals(b));
`));
await check('offscreen_culling_retains_selected_vehicle_and_pick_mapping',h=>h.run(`
  follow.id='car-B';viewFrustum.intersectsSphere=()=>false;carViewDirty=true;updateCars(.5);
  assert.equal(renderedCars.length,1);assert.equal(renderedCars[0].current.id,'car-B');assert.equal(carParts[0].mesh.count,1);
  const color=new THREE.Color();carParts[0].mesh.getColorAt(0,color);assert(Math.abs(color.r-selectedCarColor.r)<1e-6&&Math.abs(color.g-selectedCarColor.g)<1e-6&&Math.abs(color.b-selectedCarColor.b)<1e-6);
  follow.id=null;carViewDirty=true;updateCars(.5);assert.equal(renderedCars.length,0);
  assert.equal(latest.vehicles.length,2);assert.equal(cachedCars.length,2);
`));
await check('4k_render_buffer_has_pixel_budget_without_changing_css_size',h=>h.run(`
  quality='light';adaptiveScale=1;const ratio=renderPixelRatio(3840,2160);assert(3840*2160*ratio*ratio<=900001);assert(ratio<.34);
  quality='standard';const r=renderPixelRatio(3840,2160);assert(3840*2160*r*r<=1800001);
  quality='light';assert.equal(renderPixelRatio(800,600),.85);
`));
await check('combined_poll_uses_one_request_for_state_and_vehicle_trace',async h=>{
  const pending=h.run("follow.id='car-A';poll()");assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/state?vehicle=car-A');
  const state=h.run('JSON.parse(JSON.stringify(latest))');state.vehicleTrace=dossier('car-A');h.requests[0].resolve(state);await pending;
  h.run("assert.equal(follow.detail.vehicleId,'car-A');assert.equal(follow.lastFetch,latest.simTime)");assert.equal(h.requests.length,1);
});
await check('combined_poll_discards_detail_after_vehicle_switch',async h=>{
  const pending=h.run("follow.id='car-A';poll()");h.run("follow.id='car-B';follow.epoch++");
  const state=h.run('JSON.parse(JSON.stringify(latest))');state.vehicleTrace=dossier('car-A');h.requests[0].resolve(state);await pending;
  h.run("assert.equal(follow.id,'car-B');assert.equal(follow.detail,null)");
});
await check('combined_poll_aborts_when_hidden',async h=>{
  const pending=h.run("follow.id='car-A';poll()");h.run("document.hidden=true;visibilityChanged()");assert.equal(h.requests[0].signal.aborted,true);
  const state=h.run('JSON.parse(JSON.stringify(latest))');state.vehicleTrace=dossier('car-A');h.requests[0].resolve(state);await pending;
  h.run('assert.equal(follow.detail,null)');
});
await check('control_epoch_rejects_pre_command_running_response',async h=>{
  const pending=h.run("latest.status='running';follow.id='car-A';poll()");
  const oldState=h.run('JSON.parse(JSON.stringify(latest))');oldState.simTime=31;
  h.run("controlEpoch++;audioHold=true;connectionHealthy=false");
  h.requests[0].resolve(oldState);await pending;
  h.run("assert.equal(latest.simTime,30);assert.equal(connectionHealthy,false);assert.equal(audioHold,true)");
});
await check('hiding_revokes_audio_until_fresh_state_succeeds',h=>h.run(`
  const inputs=[];vehicleAudio={update:v=>inputs.push(v),dispose(){}};follow.id='car-A';follow.enabled=true;latest.status='running';connectionHealthy=true;
  document.hidden=true;visibilityChanged();assert.equal(connectionHealthy,false);assert.equal(inputs.at(-1).running,false);
  document.hidden=false;syncVehicleAudio();assert.equal(inputs.at(-1).running,false);
  connectionHealthy=true;syncVehicleAudio();assert.equal(inputs.at(-1).running,true);
  audioHold=true;syncVehicleAudio();assert.equal(inputs.at(-1).running,false);
`));

const report={passed:true,checks:checks.length,details:checks,
  scope:'Shipped frontend functions and real Three.js vectors/geometries in Node VM. Mocked DOM, renderer, frame timers, and HTTP. Not a GPU, screenshot, browser, or SUMO integration benchmark.',
  limits:{lightEffectObjects:12,followTrailPoints:120},source:'web/app.js',test:'tests/vehicle_follow.mjs'};
const evidenceDir=path.join(root,'evidence/v1_4_20261008');fs.mkdirSync(evidenceDir,{recursive:true});
fs.writeFileSync(path.join(evidenceDir,'frontend_follow_checks.json'),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report,null,2));
