import {useEffect,useRef} from 'react';import * as THREE from 'three';
const vs=`attribute vec3 b;attribute vec3 c;attribute float s;uniform float t,time,pr,mx,my;varying float a;
void main(){vec3 p=mix(position,b,smoothstep(0.,1.,clamp(t,0.,1.)));p=mix(p,c,smoothstep(0.,1.,clamp(t-1.,0.,1.)));
p.y+=sin(time*.6+p.x*1.7+s*6.)*.03;p.xy+=vec2(mx,my)*.25*s;vec4 m=modelViewMatrix*vec4(p,1.);
gl_PointSize=(1.1+s*2.)*(5./-m.z)*pr;a=.3+.7*s;gl_Position=projectionMatrix*m;}`;
const fs=`uniform vec3 ink;varying float a;void main(){float d=length(gl_PointCoord-.5);if(d>.5)discard;gl_FragColor=vec4(ink,a*smoothstep(.5,.15,d));}`;
// progress: framer MotionValue 0..1 -> morph cloud(0) -> Earth(1) -> terrain(2). mix: fixed state for static use.
export default function Particles({progress,mix=0,count=7000,ink='#ffffff',className=''}){
 const host=useRef();
 useEffect(()=>{const el=host.current,reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const r=new THREE.WebGLRenderer({alpha:true,antialias:false});r.setPixelRatio(Math.min(devicePixelRatio,2));el.appendChild(r.domElement);
  const sc=new THREE.Scene(),cam=new THREE.PerspectiveCamera(45,1,.1,50);cam.position.z=5;
  const A=new Float32Array(count*3),B=new Float32Array(count*3),C=new Float32Array(count*3),S=new Float32Array(count);const cols=Math.ceil(Math.sqrt(count*1.6));
  for(let i=0;i<count;i++){S[i]=Math.random();A.set([(Math.random()-.5)*7,(Math.random()-.5)*4,(Math.random()-.5)*4],i*3);
   const k=(i+.5)/count,ph=Math.acos(1-2*k),th=Math.PI*(1+Math.sqrt(5))*i;B.set([1.5*Math.sin(ph)*Math.cos(th),1.5*Math.cos(ph),1.5*Math.sin(ph)*Math.sin(th)],i*3);
   const x=(i%cols)/cols*7-3.5,z=Math.floor(i/cols)/(count/cols)*5-2.5;C.set([x,-1.1+Math.sin(x*1.3)*.25+Math.cos(z*1.7+x)*.2+Math.sin(x*3.1+z*2.3)*.06,z],i*3)}
  const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(A,3));g.setAttribute('b',new THREE.BufferAttribute(B,3));g.setAttribute('c',new THREE.BufferAttribute(C,3));g.setAttribute('s',new THREE.BufferAttribute(S,1));
  const u={t:{value:mix},time:{value:0},pr:{value:r.getPixelRatio()},mx:{value:0},my:{value:0},ink:{value:new THREE.Color(ink)}};
  const pts=new THREE.Points(g,new THREE.ShaderMaterial({vertexShader:vs,fragmentShader:fs,uniforms:u,transparent:true,depthWrite:false}));sc.add(pts);
  const size=()=>{const w=el.clientWidth,h=el.clientHeight;r.setSize(w,h);cam.aspect=w/h;cam.updateProjectionMatrix()};size();const ro=new ResizeObserver(size);ro.observe(el);
  let vis=true,raf,tx=0,ty=0;const io=new IntersectionObserver(([e])=>vis=e.isIntersecting);io.observe(el);
  const mv=e=>{tx=(e.clientX/innerWidth-.5)*2;ty=-(e.clientY/innerHeight-.5)*2};addEventListener('pointermove',mv);
  const off=progress?.on('change',v=>u.t.value=v*2);const clk=new THREE.Clock();
  const loop=()=>{raf=requestAnimationFrame(loop);if(!vis)return;const t=clk.getElapsedTime();u.time.value=reduce?0:t;u.mx.value+=(tx-u.mx.value)*.05;u.my.value+=(ty-u.my.value)*.05;pts.rotation.y=reduce?0:t*.04+u.mx.value*.15;pts.rotation.x=u.t.value>1.2?.35:0;r.render(sc,cam)};loop();
  return()=>{cancelAnimationFrame(raf);off&&off();ro.disconnect();io.disconnect();removeEventListener('pointermove',mv);g.dispose();r.dispose();r.domElement.remove()}},[]);
 return<div ref={host} className={'particles '+className} aria-hidden="true"/>}
