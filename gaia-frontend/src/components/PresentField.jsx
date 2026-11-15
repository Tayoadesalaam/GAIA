import {useEffect,useRef} from 'react';import * as THREE from 'three';
/* One particle field for the whole presentation. Each scene has a target formation; on a scene change
   every particle travels from where it is now to its new place, so formations morph, never reset.
   Same shader idea as ParticleStory (per-point stagger, drift, depth fade), generalised to N scenes. */
const vs=`attribute vec3 from;attribute vec3 to;attribute float rnd;
uniform float t,time,pr,amp,camd;varying float vA;
void main(){
 float k=smoothstep(0.,1.,clamp((t-rnd*.35)/.65,0.,1.));
 vec3 p=mix(from,to,k);
 p+=amp*vec3(sin(time*.21+rnd*31.),cos(time*.17+rnd*17.),sin(time*.13+rnd*7.))*(.04+.05*fract(rnd*13.));
 p+=sin(3.1416*k)*.35*(vec3(fract(rnd*7.1),fract(rnd*3.7),fract(rnd*5.3))-.5);
 vec4 mv=modelViewMatrix*vec4(p,1.);
 gl_PointSize=(.7+fract(rnd*29.)*1.9)*(5.2/-mv.z)*pr;
 vA=(.38+.62*fract(rnd*17.))*smoothstep(camd+7.,camd-2.,-mv.z);
 gl_Position=projectionMatrix*mv;}`;
const fs=`uniform float op;varying float vA;void main(){float d=length(gl_PointCoord-.5);if(d>.5)discard;gl_FragColor=vec4(vec3(.955,.955,.945),vA*op*smoothstep(.5,.08,d));}`;

const N=9000;
function rng(seed){return()=>{seed|=0;seed=seed+0x6D2B79F5|0;let t=Math.imul(seed^seed>>>15,1|seed);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
const R=rng(7);const g=()=>(R()+R()+R()-1.5)*.9;
function sphere(i,n,r,cx=0,cy=0){const k=(i+.5)/n,ph=Math.acos(1-2*k),th=Math.PI*(1+Math.sqrt(5))*i;return[cx+r*Math.sin(ph)*Math.cos(th),cy+r*Math.cos(ph),r*Math.sin(ph)*Math.sin(th)]}
function ring(r,tilt,cx,cy){const a=R()*Math.PI*2,x=Math.cos(a)*r,z=Math.sin(a)*r;return[cx+x,cy+z*Math.sin(tilt),z*Math.cos(tilt)]}
const dust=(w=18,h=10,z0=-8,z1=2)=>[(R()-.5)*w,(R()-.5)*h,z0+R()*(z1-z0)];
// scene formations (world units; camera ≈ 6.5 away, so the frame is ≈ 9.6 × 5.4)
const F=[
 // 0 opening: a planet forms, faint halo
 i=>i<N*.82?sphere(i,N*.82,1.55,2.15,.15):(()=>{const v=sphere(i,N,1.75+R()*1.1,0,0);return[v[0]+2.15,v[1]+.15,v[2]]})(),
 // 1 bigger picture: the planet recedes, orbits widen into a wider field
 i=>i<N*.38?sphere(i,N*.38,.8,2.9,.35):i<N*.8?ring([1.45,2.2,3.1][i%3],[.95,1.15,.7][i%3],2.9,.35):dust(20,11,-9,1),
 // 2 problem: three fragmented shards with debris between them
 i=>{if(i<N*.84){const c=i%3,cx=[-3.15,0,3.15][c];return[cx+g()*.55+(R()<.25?(R()-.5)*1.6:0),.95+g()*.38,g()*.6]}return dust(14,7,-6,1)},
 // 3 breakthrough: a structured lattice — an interferometric surface with concentric fringes
 i=>{const c=95,x=(i%c)/c*10-5,z=Math.floor(i/c)/(N/c)*6-4,r=Math.hypot(x+1.2,z+.8);return[x,-1.35+.18*Math.cos(r*3.2)*Math.exp(-r*.25),z]},
 // 4 solution: the field settles into a low horizon under the product
 i=>[(R()-.5)*22,-2.45+g()*.08,-9+R()*8],
 // 5 impact: an interconnected network — particles along links between nodes
 (()=>{const nodes=Array.from({length:22},()=>[(R()-.5)*10,(R()-.5)*5,(R()-.5)*2.5]);
   const edges=[];nodes.forEach((a,ai)=>{nodes.map((b,bi)=>[bi,Math.hypot(a[0]-b[0],a[1]-b[1],a[2]-b[2])]).filter(x=>x[0]!==ai).sort((p,q)=>p[1]-q[1]).slice(0,2).forEach(([bi])=>edges.push([ai,bi]))});
   return i=>{if(i<N*.3){const n=nodes[i%nodes.length];return[n[0]+g()*.12,n[1]+g()*.12,n[2]+g()*.12]}const[a,b]=edges[i%edges.length].map(k=>nodes[k]),f=R();return[a[0]+(b[0]-a[0])*f+g()*.03,a[1]+(b[1]-a[1])*f+g()*.03,a[2]+(b[2]-a[2])*f+g()*.03]}})(),
 // 6 conclusion: everything converges to one small, dense point of light
 i=>sphere(i,N,.62,0,.55),
];
const OUT=i=>dust(26,15,-14,3); // dispersal into darkness after the closing line
const VIEW=[{z:6.6,y:0,op:1},{z:7.4,y:.1,op:.9},{z:6.9,y:0,op:.85},{z:5.9,y:.9,op:.5},{z:7,y:-.2,op:.38},{z:7.2,y:0,op:.8},{z:6.4,y:0,op:1}];

export default function PresentField({scene=0,className=''}){
 const host=useRef(),api=useRef(null);
 useEffect(()=>{const el=host.current,reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;let r;
  try{r=new THREE.WebGLRenderer({alpha:true,antialias:false,powerPreference:'low-power'})}catch(e){return}
  r.setPixelRatio(Math.min(devicePixelRatio,1.75));el.appendChild(r.domElement);
  const sc=new THREE.Scene(),cam=new THREE.PerspectiveCamera(45,1,.1,60);cam.position.set(0,0,9);
  const targets=F.map(f=>{const a=new Float32Array(N*3);for(let i=0;i<N;i++)a.set(f(i),i*3);return a});
  const out=new Float32Array(N*3);for(let i=0;i<N;i++)out.set(OUT(i),i*3);
  const from=new Float32Array(N*3),to=new Float32Array(N*3),rnd=new Float32Array(N);
  for(let i=0;i<N;i++){from.set(dust(30,18,-16,4),i*3);rnd[i]=R()}to.set(targets[0]);
  const geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.BufferAttribute(new Float32Array(N*3),3));
  const aF=new THREE.BufferAttribute(from,3),aT=new THREE.BufferAttribute(to,3);geo.setAttribute('from',aF);geo.setAttribute('to',aT);geo.setAttribute('rnd',new THREE.BufferAttribute(rnd,1));
  geo.boundingSphere=new THREE.Sphere(new THREE.Vector3(),30);
  const u={t:{value:reduce?1:0},time:{value:0},pr:{value:r.getPixelRatio()},amp:{value:reduce?0:1},op:{value:reduce?1:0},camd:{value:7}};
  const mat=new THREE.ShaderMaterial({vertexShader:vs,fragmentShader:fs,uniforms:u,transparent:true,depthWrite:false,blending:THREE.AdditiveBlending});
  const pts=new THREE.Points(geo,mat);sc.add(pts);
  const fit=()=>{const w=el.clientWidth,h=el.clientHeight;if(!w||!h)return;r.setSize(w,h,false);cam.aspect=w/h;cam.updateProjectionMatrix()};fit();
  const ro=new ResizeObserver(fit);ro.observe(el);
  // state: current scene, morph clock, camera/opacity targets, closing dispersal timer
  const st={scene:0,t0:0,dur:reduce?0:3.2,view:VIEW[0],disperseAt:null,mx:0,my:0,tx:0,ty:0};
  const ease=x=>x<.5?4*x*x*x:1-Math.pow(-2*x+2,3)/2,clk=new THREE.Clock();
  // ?capture: a frame-exact clock for recording video (window.__pzStep(dt) renders one frame); otherwise real time
  const capture=new URLSearchParams(location.search).has('capture');let vt=0;const clock=()=>capture?vt:clk.getElapsedTime();
  const lerpK=(k,dt)=>1-Math.pow(1-k,dt*60); // per-frame easing made frame-rate independent (tuned at 60 fps)
  // freeze the in-flight positions into `from`, then point `to` at the new formation
  const retarget=(dest,dur)=>{const k=Math.min(1,u.t.value);for(let i=0;i<N*3;i++){const s=Math.min(1,Math.max(0,(k-rnd[(i/3)|0]*.35)/.65)),e=s*s*(3-2*s);from[i]=from[i]+(to[i]-from[i])*e}
   to.set(dest);aF.needsUpdate=aT.needsUpdate=true;u.t.value=reduce?1:0;st.t0=clock();st.dur=reduce?0:dur};
  api.current={go:s=>{if(s===st.scene)return;st.scene=s;st.view=VIEW[s];st.disperseAt=s===6?clock()+4.2:null;retarget(targets[s],s===6?2.6:2.2)}};
  const mv=e=>{st.tx=(e.clientX/innerWidth-.5)*2;st.ty=(e.clientY/innerHeight-.5)*2};if(!capture)addEventListener('pointermove',mv,{passive:true});
  let raf=0,run=true,last=0;
  const frame=()=>{const now=clock(),dt=Math.min(.1,Math.max(0,now-last));last=now;
   if(st.disperseAt&&now>st.disperseAt){st.disperseAt=null;st.view={...st.view,op:.3};retarget(out,7)}
   u.t.value=st.dur?ease(Math.min(1,(now-st.t0)/st.dur)):1;if(!reduce)u.time.value=now;
   u.op.value+=(st.view.op-u.op.value)*(reduce?1:lerpK(.025,dt));
   const kc=lerpK(.03,dt);cam.position.z+=(st.view.z-cam.position.z)*kc;cam.position.y+=(st.view.y-cam.position.y)*kc;cam.lookAt(0,st.view.y*.4,0);u.camd.value=cam.position.z;
   const km=lerpK(.035,dt);st.mx+=(st.tx-st.mx)*km;st.my+=(st.ty-st.my)*km;
   pts.rotation.y=(reduce?0:Math.sin(now*.05)*.08)+st.mx*.09;pts.rotation.x=st.my*.05;r.render(sc,cam)};
  const loop=()=>{raf=requestAnimationFrame(loop);if(run)frame()};
  if(capture)window.__pzStep=dt=>{vt+=dt;frame();return vt};else loop();
  const vis=()=>{run=!document.hidden};document.addEventListener('visibilitychange',vis);
  return()=>{cancelAnimationFrame(raf);if(capture)delete window.__pzStep;ro.disconnect();removeEventListener('pointermove',mv);document.removeEventListener('visibilitychange',vis);geo.dispose();mat.dispose();r.dispose();r.domElement.remove();api.current=null}},[]);
 useEffect(()=>{api.current?.go(scene)},[scene]);
 return<div ref={host} className={'pz-field '+className} aria-hidden="true"/>}
