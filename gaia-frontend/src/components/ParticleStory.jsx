import {useEffect,useRef} from 'react';import * as THREE from 'three';
/* One particle field, four scientific shapes. `stage` (0..3, a framer MotionValue or a number) morphs
   01 deformation surface → 02 vegetation branches → 03 flood surface → 04 Gaia sphere with orbital rings.
   Every shape is computed for every point, so the morph is a continuous interpolation, never a scene swap. */
const vs=`
attribute vec4 d0;attribute vec4 d1;attribute vec4 d2;attribute vec4 d3;attribute float rnd;
uniform float stage,time,pr,size,camd;varying vec3 vC;varying float vA;
mat3 rx(float a){float c=cos(a),s=sin(a);return mat3(1.,0.,0.,0.,c,s,0.,-s,c);}
mat3 ry(float a){float c=cos(a),s=sin(a);return mat3(c,0.,-s,0.,1.,0.,s,0.,c);}
mat3 rz(float a){float c=cos(a),s=sin(a);return mat3(c,s,0.,-s,c,0.,0.,0.,1.);}
float e(float x){return smoothstep(0.,1.,clamp((x-rnd*.3)/.7,0.,1.));}
void main(){
 float t=time;
 // 01 deformation: subsidence bowl, uplift, concentric displacement waves; fringes = interferometric phase
 vec2 q=d0.xz;float r1=length(q-vec2(-.9,.2)),r2=length(q-vec2(1.3,-.5));
 float disp=-.55*exp(-r1*r1/.55)+.32*exp(-r2*r2/.35)+.045*sin(r1*10.-t*1.4)*exp(-r1*.9);
 float base=.07*sin(q.x*1.7+q.y*.9)+.04*sin(q.x*3.3-q.y*2.1)+.02*sin(q.x*6.1+q.y*5.3);
 vec3 P0=vec3(q.x,disp+base-.2,q.y);float fr=.5+.5*cos(disp*42.);vec3 C0=vec3(.92,.93,.95)*(.25+.75*fr);
 // 02 vegetation: branches sway with height
 vec3 P1=d1.xyz;P1.x+=sin(t*.7+d1.y*2.+rnd*2.)*.035*d1.w;P1.z+=cos(t*.5+d1.y*1.7)*.02*d1.w;
 vec3 C1=mix(vec3(.5,.62,.46),vec3(.86,.95,.78),d1.w);
 // 03 flooding: rippled water, exposed ground stays still
 float w=d2.w;vec2 g=d2.xz;float rip=sin(length(g-vec2(-1.,.4))*9.-t*1.8)*.5+sin(length(g-vec2(1.6,-.8))*7.-t*1.3)*.5;
 vec3 P2=vec3(g.x,mix(d2.y,-.3+rip*.035,w),g.y);vec3 C2=mix(vec3(.82,.78,.7)*.85,vec3(.55,.72,1.)*(.55+.3*rip),w);
 // 04 Gaia: sphere, three thin orbital rings, drifting particles
 vec3 P3;vec3 C3;float k=d3.w;
 if(k<.5){P3=ry(t*.08)*d3.xyz*1.15;C3=vec3(.95);}
 else if(k<1.5){int id=int(d3.z+.5);float sp=id==0?.12:id==1?.08:-.06;float a=d3.x+t*sp;vec3 c=vec3(cos(a)*d3.y,0.,sin(a)*d3.y);
  P3=id==0?rx(1.15)*c:id==1?rz(.5)*rx(.35)*c:rz(-.3)*rx(-.9)*c;C3=vec3(.78);}
 else{P3=d3.xyz+.06*vec3(sin(t*.3+rnd*6.),cos(t*.25+rnd*5.),sin(t*.2+rnd*4.));C3=vec3(.5);}
 float e1=e(stage),e2=e(stage-1.),e3=e(stage-2.);
 vec3 p=mix(mix(mix(P0,P1,e1),P2,e2),P3,e3);vec3 col=mix(mix(mix(C0,C1,e1),C2,e2),C3,e3);
 float m=sin(3.1416*e1)+sin(3.1416*e2)+sin(3.1416*e3);p+=m*.28*(vec3(rnd,fract(rnd*7.13),fract(rnd*13.7))-.5);
 vec4 mv=modelViewMatrix*vec4(p,1.);gl_PointSize=(1.+fract(rnd*31.)*1.5)*(size/-mv.z)*pr;
 vC=col;vA=(.45+.55*fract(rnd*17.))*smoothstep(camd+4.5,camd-2.5,-mv.z);gl_Position=projectionMatrix*mv;}`;
const fs=`varying vec3 vC;varying float vA;void main(){float d=length(gl_PointCoord-.5);if(d>.5)discard;gl_FragColor=vec4(vC,vA*smoothstep(.5,.1,d));}`;

function build(N){
 const R=Math.random,d0=new Float32Array(N*4),d1=new Float32Array(N*4),d2=new Float32Array(N*4),d3=new Float32Array(N*4),rnd=new Float32Array(N);
 const cols=Math.ceil(Math.sqrt(N*1.6));
 // vegetation: recursive branching trees + crop rows; trees differ in vigour (density)
 const segs=[],tips=[];
 const tree=(x,z,h,depth,v)=>{const go=(p,dir,len,d)=>{const q=p.map((c,i)=>c+dir[i]*len);segs.push([p,q,len*v]);if(d>=depth){tips.push([q,v]);return}
  const n=d<2?2:2+(R()<.4);for(let i=0;i<n;i++){const a=(R()-.5)*1.4,b=R()*Math.PI*2;const nd=[dir[0]+Math.sin(a)*Math.cos(b),dir[1]*.9+.15,dir[2]+Math.sin(a)*Math.sin(b)];const l=Math.hypot(...nd);go(q,nd.map(c=>c/l),len*.72,d+1)}};
  go([x,-1.05,z],[0,1,0],h*.38,0)};
 [[-2.4,-.6,1.5,.9],[-1.3,.7,1.9,1.3],[-.2,-.9,1.2,.6],[.7,.5,2.1,1.4],[1.8,-.4,1.4,.8],[2.6,.9,1.1,.5],[-2.9,1,1,.4],[.1,1.3,1.3,.7],[2.2,-1.4,.9,.35]].forEach(([x,z,h,v])=>tree(x,z,h,5,v));
 const segW=segs.reduce((s,a)=>s+a[2],0),tipW=tips.reduce((s,a)=>s+a[1],0);
 const pick=(arr,wi,W)=>{let r=R()*W;for(const a of arr){r-=a[wi];if(r<=0)return a}return arr[arr.length-1]};
 const gauss=()=>(R()+R()+R()-1.5)*.55;
 for(let i=0;i<N;i++){const r=R();rnd[i]=r;
  // shared jittered grid for both surfaces
  const x=((i%cols)+R()*.6)/cols*6.6-3.3,z=(Math.floor(i/cols)+R()*.6)/(N/cols)*4.2-2.1;
  d0.set([x,0,z,0],i*4);
  const h=.25*Math.sin(x*1.1+.5)+.2*Math.cos(z*1.6+x*.7)+.1*Math.sin(x*2.9+z*2.3)+.06*Math.sin(x*5.7-z*4.1),wet=h<.05;
  d2.set([x,wet?0:h-.3+.05,z,wet?1:0],i*4);
  const u=R();
  if(u<.32){const[s,e]=pick(segs,2,segW),f=R();const p=s.map((c,j)=>c+(e[j]-c)*f+(R()-.5)*.015);d1.set([...p,(p[1]+1.05)/2.6],i*4)}
  else if(u<.86){const[c,v]=pick(tips,1,tipW),sg=.07+.05*v;const p=[c[0]+gauss()*sg*2,c[1]+gauss()*sg*1.6,c[2]+gauss()*sg*2];d1.set([...p,Math.min(1,(p[1]+1.05)/2.6+.2)],i*4)}
  else{const row=Math.floor(R()*9);d1.set([R()*6.4-3.2,-1.05+R()*.05,-1.8+row*.45+(R()-.5)*.04,0],i*4)}
  const k=R();
  if(k<.62){const kk=(i+.5)/N,ph=Math.acos(1-2*kk),th=Math.PI*(1+Math.sqrt(5))*i;d3.set([Math.sin(ph)*Math.cos(th),Math.cos(ph),Math.sin(ph)*Math.sin(th),0],i*4)}
  else if(k<.86){const id=Math.floor(R()*3);d3.set([R()*Math.PI*2,[1.55,1.85,2.2][id]+(R()-.5)*.016,id,1],i*4)}
  else{const a=R()*Math.PI*2,b=Math.acos(2*R()-1),rr=1.35+R()*1.5;d3.set([rr*Math.sin(b)*Math.cos(a),rr*Math.cos(b),rr*Math.sin(b)*Math.sin(a),2],i*4)}}
 const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(new Float32Array(N*3),3));
 [['d0',d0],['d1',d1],['d2',d2],['d3',d3]].forEach(([n,a])=>g.setAttribute(n,new THREE.BufferAttribute(a,4)));g.setAttribute('rnd',new THREE.BufferAttribute(rnd,1));
 g.boundingSphere=new THREE.Sphere(new THREE.Vector3(),6);return g}

const TILT=[.62,.24,.55,.1];
export default function ParticleStory({stage=0,count=14000,camZ=5.4,size=6,className=''}){
 const host=useRef();
 useEffect(()=>{const el=host.current,reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;let r;
  try{r=new THREE.WebGLRenderer({alpha:true,antialias:false,powerPreference:'low-power'})}catch(e){el.classList.add('no-gl');return}
  r.setPixelRatio(Math.min(devicePixelRatio,1.75));el.appendChild(r.domElement);
  const sc=new THREE.Scene(),cam=new THREE.PerspectiveCamera(45,1,.1,40);cam.position.z=camZ;
  const g=build(count),read=()=>typeof stage==='number'?stage:stage.get();
  const u={stage:{value:read()},time:{value:reduce?2:0},pr:{value:r.getPixelRatio()},size:{value:size},camd:{value:camZ}};
  const mat=new THREE.ShaderMaterial({vertexShader:vs,fragmentShader:fs,uniforms:u,transparent:true,depthWrite:false,blending:THREE.AdditiveBlending});
  const pts=new THREE.Points(g,mat);sc.add(pts);
  const fit=()=>{const w=el.clientWidth,h=el.clientHeight;if(!w||!h)return;r.setSize(w,h,false);cam.aspect=w/h;cam.position.z=camZ*Math.max(1,1.25/cam.aspect);cam.updateProjectionMatrix();u.camd.value=cam.position.z};
  fit();const ro=new ResizeObserver(fit);ro.observe(el);
  let vis=false,raf=0,mx=0,my=0,tx=0,ty=0;const clk=new THREE.Clock();
  const mv=e=>{tx=(e.clientX/innerWidth-.5)*2;ty=(e.clientY/innerHeight-.5)*2};addEventListener('pointermove',mv,{passive:true});
  const loop=()=>{raf=0;if(!vis)return;raf=requestAnimationFrame(loop);const t=clk.getElapsedTime(),tgt=read();
   u.stage.value=reduce?tgt:u.stage.value+(tgt-u.stage.value)*.09;if(!reduce)u.time.value=t;
   mx+=(tx-mx)*.04;my+=(ty-my)*.04;const s=Math.min(3,Math.max(0,u.stage.value)),i=Math.min(2,Math.floor(s)),f=s-i;
   pts.rotation.x=TILT[i]+(TILT[i+1]-TILT[i])*f+my*.06;pts.rotation.y=(reduce?0:Math.sin(t*.07)*.18)+mx*.12;r.render(sc,cam)};
  const io=new IntersectionObserver(([e])=>{vis=e.isIntersecting;if(vis&&!raf){clk.getDelta();loop()}});io.observe(el);
  return()=>{cancelAnimationFrame(raf);ro.disconnect();io.disconnect();removeEventListener('pointermove',mv);g.dispose();mat.dispose();r.dispose();r.domElement.remove()}},[]);
 return<div ref={host} className={'pstory '+className} aria-hidden="true"/>}
