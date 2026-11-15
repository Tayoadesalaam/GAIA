import {useCallback,useEffect,useState} from 'react';import {Link} from 'react-router-dom';import {AnimatePresence,motion} from 'framer-motion';
import PresentField from '../components/PresentField';import Logo from '../components/Logo';import '../present.css';
/* GAIA — Team HunterX. Seven scenes over one shared particle field. Every figure here is a real GAIA
   result or measurement (Lagos, NISAR track 152, Jun–Oct 2026; Karakum desert test), as in the deck. */
const ease=[.16,1,.3,1];
// a staged element: appears `d` seconds after its scene arrives
const In=({d=0,y=22,as='div',className='',children,...p})=>{const M=motion[as];return<M className={className} initial={{opacity:0,y,filter:'blur(8px)'}} animate={{opacity:1,y:0,filter:'blur(0px)'}} transition={{duration:1.1,delay:d,ease}} {...p}>{children}</M>};

function Opening(){return<div className="pz-s pz-open">
 <In d={1.6} as="p" className="pz-eyebrow">Team HunterX · NASA Space Apps Challenge 2026</In>
 <In d={2.0} as="h1" className="pz-h1">Know the ground<br/>before it moves.</In>
 <In d={2.7} as="p" className="pz-lede">GAIA reads NASA–ISRO NISAR radar for any area on Earth.</In>
 <In d={3.1} as="p" className="pz-mono pz-dim">Challenge · Dancing with the SARs</In>
</div>}

const CONTEXT=[['12 days','to revisit the same ground'],['Day & night','radar needs no sunlight'],['Through cloud','radar sees through cloud cover'],['20 passes','over Lagos since 24 Jun 2026, on two tracks']];
function Context(){return<div className="pz-s pz-context">
 <In d={.6} as="p" className="pz-eyebrow">02 · The bigger picture</In>
 <In d={.9} as="h2" className="pz-h2">NISAR sees every<br/>change on Earth.</In>
 <In d={1.4} as="p" className="pz-lede">A joint NASA–ISRO radar mission, watching the surface for water, crops and ground motion.</In>
 <div className="pz-stats">{CONTEXT.map((c,i)=><In key={c[0]} d={1.9+i*.28} className="pz-stat"><motion.i className="pz-rule" initial={{scaleX:0}} animate={{scaleX:1}} transition={{duration:1,delay:1.9+i*.28,ease}}/><b>{c[0]}</b><span>{c[1]}</span></In>)}</div>
</div>}

const PROBLEMS=[['6 GB','Too big to use','One NISAR radar file, for a single pass.'],['Water, or not?','Easy to misread','Wet soil, tarmac and desert sand look as dark as water.'],['Noise, or news?','Silent about doubt','A few months of interferograms are mostly noise.']];
function Problem(){return<div className="pz-s pz-problem">
 <In d={.7} as="p" className="pz-eyebrow">03 · The problem</In>
 <In d={1.0} as="h2" className="pz-h2">Few people can read it.</In>
 <div className="pz-trio">{PROBLEMS.map((p,i)=><In key={p[0]} d={1.6+i*.45} className="pz-frag"><span className="pz-mono pz-dim">{p[1]}</span><b>{p[0]}</b><span>{p[2]}</span></In>)}</div>
 <motion.i className="pz-link" initial={{scaleX:0}} animate={{scaleX:1}} transition={{duration:1.6,delay:3.1,ease}}/>
 <In d={3.6} as="p" className="pz-lede pz-center">Raw passes are huge, easy to misread, and never say how sure they are.</In>
</div>}

const LAYERS=[['Stream the area, not the file','190 MB of a 6 GB pass, read for Lagos in about 2.5 minutes.'],['Calibrate on known water and dry land','Lagos: 91% hit rate, 0% false alarms.'],['Flag rain-soaked passes','GPM IMERG rain: 4 of 10 Lagos passes kept out of the peak.']];
function Breakthrough(){return<div className="pz-s pz-break">
 <div className="pz-break-l">
  <In d={.5} as="p" className="pz-eyebrow">04 · The breakthrough</In>
  <In d={.8} as="h2" className="pz-h2">Read only what matters.<br/>Say how sure.</In>
  <ol className="pz-layers">{LAYERS.map((l,i)=><In key={l[0]} as="li" d={1.8+i*1.1}><span className="pz-mono">0{i+1}</span><div><b>{l[0]}</b><span>{l[1]}</span></div></In>)}</ol>
 </div>
 <div className="pz-viz">
  <In d={.3} y={0} className="pz-map" role="img" aria-label="Real GAIA map of Lagos: the analysed area outlined, with NISAR peak-flood water along the lagoon">
   <img className="pz-map-gray" src="/present/lagos-flood-map.jpg" alt=""/>
   <motion.img className="pz-map-color" src="/present/lagos-flood-map.jpg" alt="" initial={{clipPath:'circle(0% at 73.75% 56%)'}} animate={{clipPath:'circle(21% at 73.75% 56%)'}} transition={{duration:1.8,delay:3.0,ease}}/>
   <motion.i className="pz-aoi" initial={{opacity:0,scale:1.08}} animate={{opacity:1,scale:1}} transition={{duration:1.2,delay:1.9,ease}}/>
   <In d={2.2} as="span" className="pz-tag" style={{left:'32.75%',top:'29%'}}>Selected area · 2,348 km²</In>
  </In>
  <In d={4.2} className="pz-insight"><b>11.3 km²</b><span>peak flood beyond normal water · 22 Oct 2026 · medium confidence</span></In>
 </div>
</div>}

const CALLOUTS=[{n:1,t:'Search a place or draw an area',s:'The satellite map, with NISAR results laid over it.',box:[21.5,17.4,54.9,48.9]},{n:2,t:'Four engines, one area',s:'Water change, flooding, vegetation and deformation, each with its finding.',box:[77.4,17.4,21.5,64.2]},{n:3,t:'Confidence on every result',s:'High, medium or low, with the reasons on each tab.',box:[1.1,50.6,19.4,23.3]},{n:4,t:'Rain checked on every pass',s:'Passes after heavy rain are flagged, not trusted.',box:[1.1,75.6,19.4,24.4]}];
function Solution(){const[step,setStep]=useState(0);
 // focus each part of the interface in turn (1–4), then show all four together (5)
 useEffect(()=>{if(matchMedia('(prefers-reduced-motion: reduce)').matches){setStep(5);return}
  let k=0,t;const tick=()=>{k++;setStep(k);if(k<5)t=setTimeout(tick,1700)};t=setTimeout(tick,1500);return()=>clearTimeout(t)},[]);
 return<div className="pz-s pz-sol">
 <div className="pz-sol-head"><In d={.4} as="p" className="pz-eyebrow">05 · The solution</In><In d={.7} as="h2" className="pz-h2">One map. Four engines. An honest answer.</In></div>
 <motion.div className="pz-ui" initial={{opacity:0,y:40,rotateX:10,scale:.96}} animate={{opacity:1,y:0,rotateX:0,scale:1}} transition={{duration:1.4,delay:.6,ease}}>
  <img src="/shots/overview.jpg" alt="The GAIA dashboard for Lagos: area summary, confidence per engine, rain before passes, the satellite map with the analysed area, and key findings for all four engines"/>
  {CALLOUTS.map(c=><motion.i key={c.n} className={'pz-hl'+(step===c.n?' on':'')} style={{left:c.box[0]+'%',top:c.box[1]+'%',width:c.box[2]+'%',height:c.box[3]+'%'}} initial={{opacity:0}} animate={{opacity:step>=c.n?1:0}} transition={{duration:.6}}><em>{c.n}</em></motion.i>)}
 </motion.div>
 <ol className="pz-calls">{CALLOUTS.map(c=><motion.li key={c.n} initial={{opacity:0,x:16}} animate={{opacity:step>=c.n?(step===c.n||step>4?1:.45):0,x:step>=c.n?0:16}} transition={{duration:.6,ease}}><span className="pz-mono">{c.n}</span><div><b>{c.t}</b><span>{c.s}</span></div></motion.li>)}</ol>
</div>}

const METRICS=[['190 MB','streamed from a 6 GB pass for Lagos'],['0.5 km²','false “water” left in the Karakum desert, down from 399 km²'],['4 of 10','rain-affected Lagos passes kept out of the flood peak']];
const ENG=['Flooding & water change','Vegetation','Deformation'],PEOPLE=['Insurers','Response teams','Farmers','City planners'],LINKS=[[0,0],[0,1],[1,2],[2,3]];
function Impact(){const ey=i=>60+i*90,py=i=>40+i*80;return<div className="pz-s pz-impact">
 <In d={.4} as="p" className="pz-eyebrow">06 · The impact</In>
 <In d={.7} as="h2" className="pz-h2">Less data. Fewer false alarms.<br/>Doubt left in.</In>
 <div className="pz-metrics">{METRICS.map((m,i)=><In key={m[0]} d={1.3+i*.3}><b>{m[0]}</b><span>{m[1]}</span></In>)}</div>
 <div className="pz-net" role="img" aria-label="Flooding and water change serve insurers and response teams; vegetation serves farmers; deformation serves city planners">
  <svg viewBox="0 0 1000 340" preserveAspectRatio="none" aria-hidden="true">{LINKS.map(([e,p],i)=><motion.path key={i} d={`M300 ${ey(e)} C 500 ${ey(e)}, 500 ${py(p)}, 700 ${py(p)}`} fill="none" stroke="#f3f3f1" strokeOpacity=".55" strokeWidth="1" initial={{pathLength:0}} animate={{pathLength:1}} transition={{duration:1.2,delay:2.6+i*.25,ease}}/>)}</svg>
  {/* outer div centres each label on its line; the animated inner element owns its own transform */}
  {ENG.map((e,i)=><div key={e} className="pz-node pz-node-l" style={{top:(ey(i)/340*100)+'%'}}><In d={2.2+i*.15}>{e}</In></div>)}
  {PEOPLE.map((p,i)=><div key={p} className="pz-node pz-node-r" style={{top:(py(i)/340*100)+'%'}}><In d={3.2+i*.15}>{p}</In></div>)}
 </div>
 <In d={4.2} as="p" className="pz-lede pz-take">A reading for anyone who has to decide about the ground.</In>
</div>}

function Closing(){return<div className="pz-s pz-close">
 <In d={2.4} as="h2" className="pz-h1 pz-close-h">Know the ground<br/>before it moves.</In>
 <In d={3.3} className="pz-close-id"><Logo className="pz-close-logo"/><span className="pz-mono pz-dim">Team HunterX · NASA Space Apps Challenge 2026</span></In>
</div>}

const SCENES=[['Opening',Opening],['The bigger picture',Context],['The problem',Problem],['The breakthrough',Breakthrough],['The solution',Solution],['The impact',Impact],['Conclusion',Closing]];
const fromHash=()=>{const n=parseInt(location.hash.slice(1),10);return n>=1&&n<=7?n-1:0};

export default function Present(){
 const[i,setI]=useState(fromHash);
 const go=useCallback(n=>setI(Math.max(0,Math.min(SCENES.length-1,n))),[]);
 useEffect(()=>{history.replaceState(null,'','#'+(i+1));document.title=`GAIA · ${SCENES[i][0]}`},[i]);
 useEffect(()=>{const k=e=>{if(e.target.closest('input,textarea'))return;
   if(['ArrowRight','PageDown',' ','Enter'].includes(e.key)){e.preventDefault();setI(v=>Math.min(6,v+1))}
   else if(['ArrowLeft','PageUp','Backspace'].includes(e.key)){e.preventDefault();setI(v=>Math.max(0,v-1))}
   else if(e.key==='Home')setI(0);else if(e.key==='End')setI(6);
   else if(e.key==='f'||e.key==='F'){document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen?.().catch(()=>{})}};
  addEventListener('keydown',k);let x0=null;const ts=e=>{x0=e.touches[0].clientX},te=e=>{if(x0==null)return;const dx=e.changedTouches[0].clientX-x0;if(Math.abs(dx)>50)setI(v=>Math.max(0,Math.min(6,v+(dx<0?1:-1))));x0=null};
  addEventListener('touchstart',ts,{passive:true});addEventListener('touchend',te);
  return()=>{removeEventListener('keydown',k);removeEventListener('touchstart',ts);removeEventListener('touchend',te)}},[]);
 const S=SCENES[i][1];
 return<main className="pz" aria-roledescription="presentation" aria-label="GAIA — Team HunterX">
  <PresentField scene={i}/>
  <div className="pz-vignette" aria-hidden="true"/>
  <AnimatePresence mode="wait"><motion.section key={i} className="pz-stage" aria-roledescription="slide" aria-label={`${i+1} of 7: ${SCENES[i][0]}`}
   initial={{opacity:1}} animate={{opacity:1}} exit={{opacity:0,filter:'blur(10px)',transition:{duration:.55,ease}}}
   onClick={e=>{if(!e.target.closest('a,button'))go(i+1)}}><S/></motion.section></AnimatePresence>
  <nav className="pz-nav" aria-label="Slides">
   <Link to="/" className="pz-home" aria-label="Back to the GAIA site"><Logo/></Link>
   <div className="pz-ticks">{SCENES.map((s,n)=><button key={n} className={n===i?'on':''} onClick={()=>go(n)} aria-label={`Slide ${n+1}: ${s[0]}`} aria-current={n===i?'step':undefined}/>)}</div>
   <span className="pz-mono pz-dim pz-count">{String(i+1).padStart(2,'0')} / 07</span>
  </nav>
  <p className="pz-sr" aria-live="polite">{`Slide ${i+1} of 7: ${SCENES[i][0]}`}</p>
 </main>}
