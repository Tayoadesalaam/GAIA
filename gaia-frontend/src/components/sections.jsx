import {useState,useRef,useEffect} from 'react';import {Link,useNavigate} from 'react-router-dom';import {motion,AnimatePresence,useScroll,useMotionValueEvent,useTransform} from 'framer-motion';
import {Cell,Sec,Reveal,Words,ImgSlot} from '../lib/grid';import ParticleStory from './ParticleStory';import Logo from './Logo';import {NAV,FAQ,SOURCES} from '../data/content';import {loadMonitors} from '../lib/assistant';
export function Header(){const[h,setH]=useState(false);const[o,setO]=useState(null);const{scrollY}=useScroll();const last=useRef(0);
 useMotionValueEvent(scrollY,'change',v=>{setH(v>last.current&&v>200);last.current=v});
 return<motion.header className="header" animate={{y:h?'-110%':0}} transition={{duration:.4,ease:[.16,1,.3,1]}}><Link to="/" className="brand" aria-label="Gaia home"><Logo/></Link>
 <nav className="pill" onMouseLeave={()=>setO(null)}>{NAV.map(n=><div key={n.l} className="dd" onMouseEnter={()=>setO(n.kids?n.l:null)}><Link to={n.to}>{n.l}{n.kids&&' ▾'}</Link>
  <AnimatePresence>{o===n.l&&<motion.div className="menu" initial={{opacity:0,y:8}} animate={{opacity:1,y:0}} exit={{opacity:0,y:8}} transition={{duration:.2}}>{n.kids.map(k=><Link key={k[0]} to={k[0]} onClick={()=>setO(null)}><b>{k[1]}</b><small>{k[3]}</small></Link>)}</motion.div>}</AnimatePresence></div>)}</nav>
 <span className="sp"/><Link className="btn" to="/dashboard">Dashboard</Link><Link className="btn solid" to="/request-demo">Sign up</Link></motion.header>}
export const LogoWall=()=><Sec bg="neue" className="logowall"><Cell><div className="marq"><div className="track">{[...SOURCES,...SOURCES,...SOURCES,...SOURCES].map((s,i)=><span key={i} className="mono">{s}</span>)}</div></div></Cell></Sec>;
// North .c-layer: optional asset row (homepage: Gaia Assistant), title row, then 2x2 block (description | lines / illustration | subblock)
export const Layer=({title,desc,subs,slot,shot,asset,bg='neue',children})=><Sec bg={bg} className="layer">
 {asset&&<Cell className="layer-asset">{asset}</Cell>}
 <Cell className="layer-header"><Words text={title}/></Cell>
 <Cell span={17} className="layer-desc"><Reveal><p className="lede">{desc}</p></Reveal></Cell>
 <Cell span={17} className="layer-lines" nb="nl">{children||<ImgSlot label={slot+' · chart'} shot={shot&&shot[1]} ratio="16/7"/>}</Cell>
 <Cell span={17} className="layer-ill"><ImgSlot label={slot} shot={shot&&shot[0]} ratio="4/3"/></Cell>
 <div className="c17 sub2"><Cell span={17} nb="nl nb2"><p>{subs[0]}</p></Cell><Cell span={17} nb="nb2"><p>{subs[1]}</p></Cell></div></Sec>;
export const Numbers=({items})=><div className="nums">{items.map(n=><div key={n[1]}><div className="big">{n[0]}<small>{n[2]}</small></div><span className="mono">{n[1]}</span></div>)}</div>;
// North .c-scrollAccordion: tall track, sticky pin. Scroll drives the particle story; the intro slides up under the indicator
// while the visual (.acc-assets) and the list (.acc-data) stay anchored.
export function Accordion({items}){const ref=useRef();const[a,setA]=useState(0);const{scrollYProgress:p}=useScroll({target:ref,offset:['start start','end end']});const N=items.length;
 useMotionValueEvent(p,'change',v=>setA(Math.min(N-1,Math.floor(v*N*.999))));
 const bar=useTransform(p,[0,1],['0%','100%']),introY=useTransform(p,[0,.14],['0%','-105%']),introO=useTransform(p,[0,.1,.14],[1,.6,0]);
 // hold each shape for most of its segment, morph in the last third
 const stage=useTransform(p,v=>{const x=Math.min(N-1,Math.max(0,v*N)),i=Math.min(N-2,Math.floor(x)),f=Math.min(1,x-i);const s=x>=N-1?N-1:i+(f<.62?0:(f-.62)/.38);return Math.min(3,s*3/(N-1))});
 return<section ref={ref} data-bg="black" className="acc" style={{height:`${N*90+40}vh`}}><div className="acc-pin"><div className="px"><div className="g">
 <Cell className="acc-h"><span className="mono">The Platform</span><span className="mono acc-n">0{a+1} / 0{N}</span><div className="rail"><motion.i style={{width:bar}}/></div></Cell>
 <div className="c34 acc-intro"><motion.div className="acc-intro-in" style={{y:introY,opacity:introO}}>
  <Cell span={17} nb="nr nt"><Words as="h2" className="t3" text="One platform for every ground decision."/></Cell><Cell span={17} nb="nt"><p className="lede">Gaia turns signal into a reading and a next step, from radar phase to planting window.</p></Cell></motion.div></div>
 <Cell span={17} className="acc-assets"><ParticleStory stage={stage}/><span className="mono acc-cap">{items[a][0]}</span></Cell>
 <Cell span={17} className="acc-data">{items.map((it,i)=><div key={i} className={'di'+(i===a?' on':'')}><span className="mono">0{i+1} · {it[0]}</span><h3 className="t5">{it[1]}</h3><motion.p initial={false} animate={{height:i===a?'auto':0,opacity:i===a?1:0}} transition={{duration:.4}}>{it[2]}</motion.p></div>)}</Cell>
 </div></div></div></section>}
// A real reading: the most recent finished Lagos analysis on the backend (falls back to any finished one).
function useReading(){const[r,setR]=useState(undefined);useEffect(()=>{let live=true;(async()=>{const ms=await loadMonitors();if(!ms){live&&setR(null);return}
 const done=ms.filter(m=>m.last_run_status==='done'||m.last_run_status==='partial'),m=done.find(x=>/lagos/i.test(x.name+x.query))||done[0];if(!m){live&&setR(null);return}
 try{const d=await(await fetch('/api/monitors/'+m.id)).json();live&&setR({m,R:d.last_job?.results||{}})}catch{live&&setR(null)}})();return()=>{live=false}},[]);return r}
const fmt=(v,d=1)=>v==null||isNaN(v)?'—':(+v).toLocaleString('en-GB',{maximumFractionDigits:d});
const dd=s=>s?new Date(s+'T00:00:00Z').toLocaleDateString('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}):'';
export function Stories(){const r=useReading(),R=r?.R||{},name=r?(r.m.name||r.m.query).split(',')[0]:'Lagos';
 const fl=R.flood?.stats,wc=R.water_change?.stats,de=R.deformation?.stats;
 const items=[fl&&[fmt(fl.peak_flood_total_km2,1),'Flood peak · '+dd(fl.peak_date),'km²'],wc&&[fmt(wc.new_water_km2,1),'New water · '+R.water_change.dates.map(dd).join('–'),'km²'],
  de&&[fmt(de.velocity_mm_yr_percentiles?.['50'],1),`Ground motion (±${fmt(de.velocity_noise_mm_yr,0)} noise)`,'mm/yr']].filter(Boolean);
 return<Sec bg="black" className="stories"><Cell span={17} nb="nr"><Words as="h2" className="t5" text={`${name}: a real reading from NISAR radar, with its uncertainty attached.`}/>
  {r&&<p className="st-note">Confidence: {Object.entries(R).map(([k,v])=>`${k.replace('_',' ')} ${v.confidence}`).join(' · ')}.{de&&Math.abs(de.velocity_mm_yr_percentiles?.['50'])<de.velocity_noise_mm_yr?` Ground motion is below the ±${fmt(de.velocity_noise_mm_yr,0)} mm/yr noise after ${fmt(de.span_days,0)} days, so no deformation signal is claimed.`:''}</p>}</Cell>
 <Cell span={17}>{items.length?<Numbers items={items}/>:<p className="st-note">{r===undefined?'Reading the latest analysis…':'Start the GAIA backend to show the latest analysed reading here.'}</p>}</Cell>
 <Cell span={17}><ImgSlot label={name+' · flood peak map'} shot="flooding" ratio="4/3"/></Cell>
 <Cell span={17} className="quote"><p className="t6">“The best time to see ground move is before it does.”</p><span className="mono">Gaia team · NASA Space Apps 2026</span><Link className="btn solid" to={r?`/dashboard?monitor=${r.m.id}`:'/dashboard'}>Open this reading</Link></Cell></Sec>}
export const Savings=({head=['Meaningful Insight','In Seconds.'],rows})=><Sec bg="neue" className="savings"><Cell className="sv-h">{head.map(h=><Words key={h} as="h2" className="t2" text={h}/>)}</Cell>
 <Cell nb="nb"><div className="sv-t"><span className="mono">Benefits</span><div className="g2"><div className="cell cc big">1 search</div><div className="sub2 c17"><div className="cell"><p>One place returns deformation, vegetation and flood readings.</p></div><div className="cell"><p>Every reading carries its confidence and limits.</p></div></div></div></div></Cell>
 {rows.map((r,i)=><Cell key={i} className="row"><Reveal className="row-item"><ImgSlot label={r[0]} shot={r[2]} ratio="1/1" className="row-asset"/><h3 className="t5">{r[0]}</h3><p>{r[1]}</p></Reveal></Cell>)}</Sec>;
export function Comparison({rows}){const[w,setW]=useState(true);return<Sec bg="neue" className="cmp"><Cell span={17} nb="nt"><Words as="h2" className="t3" text="Land risk used to run on guesswork."/><p className="t6">Now it runs on Gaia.</p></Cell>
 <Cell span={17}><div className="tbl"><div className="tog">{['With Gaia','Without'].map((l,i)=><button key={l} className={(i===0)===w?'on':''} onClick={()=>setW(i===0)}>{l}</button>)}</div>
 <AnimatePresence mode="wait"><motion.div key={w} initial={{opacity:0,y:10}} animate={{opacity:1,y:0}} exit={{opacity:0}} transition={{duration:.25}}>{rows.map(r=><div className="tr" key={r[0]}><span className="mono">{r[0]}</span><b>{w?r[2]:r[1]}</b></div>)}</motion.div></AnimatePresence></div></Cell></Sec>}
export const Grit=()=><Sec bg="grey" className="grit"><Cell><p className="lede">Open data, honest limits. Sourced and ready to explain.</p></Cell>{[['Sources','NISAR GCOV and GUNW radar, GPM IMERG rainfall, JRC surface water.'],['Latency','NISAR repeats every 12 days; each result lists its pass dates.'],['Resolution','10–30 m radar pixels, clipped to the area you draw.'],['Status','Live NISAR data; PROVISIONAL products are flagged.'],['Privacy','No account needed; place search uses OpenStreetMap.'],['Export','Every map downloads as a Cloud Optimized GeoTIFF.']].map(g=><Cell key={g[0]} span={11} className="gc"><Reveal><span className="mono">{g[0]}</span><p>{g[1]}</p></Reveal></Cell>)}</Sec>;
export function Faqs(){const[o,s]=useState(0);return<Sec bg="neue" className="faq"><Cell><Words text="Questions" as="h2"/></Cell>{FAQ.map((f,i)=><Cell key={i}><button className="q" onClick={()=>s(o===i?-1:i)} aria-expanded={o===i}><h3 className="t5">{f[0]}</h3><span>{o===i?'−':'+'}</span></button><motion.div initial={false} animate={{height:o===i?'auto':0,opacity:o===i?1:0}} transition={{duration:.4,ease:[.16,1,.3,1]}} style={{overflow:'hidden'}}><p className="a">{f[1]}</p></motion.div></Cell>)}</Sec>}
export function GetStarted(){const[q,setQ]=useState('');const nav=useNavigate();const go=p=>nav(p.trim()?'/dashboard?q='+encodeURIComponent(p.trim()):'/dashboard');return<Sec bg="black" className="gs"><Cell><Words text="Start reading the ground." className="t1"/>
 <form onSubmit={e=>{e.preventDefault();go(q)}} className="gs-f"><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search any location" aria-label="Search any location"/><button className="btn solid">Open dashboard</button></form>
 <div className="chips">{['Lagos','Kano','Jos','Nairobi','Jakarta','Dhaka'].map(c=><Link key={c} to={'/dashboard?q='+c} className="btn">{c}</Link>)}</div></Cell></Sec>}
export const Footer=()=><Sec bg="black" className="foot"><Cell span={14}><Link to="/" className="brand" aria-label="Gaia home"><Logo/></Link><p className="mono">Land, vegetation and flood intelligence from NASA NISAR radar.</p></Cell>
 {[['Product',[['Overview','/features/overview'],['Hazards','/use-cases/flood-watch'],['Dashboard','/dashboard']]],['Developers',[['Data sources','/integrations'],['NISAR','/integrations/nisar']]],['Company',[['Why Gaia','/why-gaia'],['Hackathon','/hackathon'],['Privacy','/privacy']]]].map(c=><Cell key={c[0]} span={7}><span className="mono">{c[0]}</span>{c[1].map(l=><Link key={l[1]} to={l[1]}>{l[0]}</Link>)}</Cell>)}</Sec>;
