import {useEffect,useRef,useState} from 'react';import {Link} from 'react-router-dom';import {AnimatePresence,motion} from 'framer-motion';
import ParticleStory from './ParticleStory';import {ask,askAI,aiStatus,loadMonitors} from '../lib/assistant';
// Minimal safe Markdown for AI replies: React escapes text; we only split bold and list items.
const inl=t=>t.split(/(\*\*[^*]+\*\*)/g).map((x,i)=>/^\*\*.+\*\*$/.test(x)?<b key={i}>{x.slice(2,-2)}</b>:x.replace(/(^|\s)_([^_]+)_(?=\s|$)/g,'$1$2'));
const MD=({text})=>{const L=[],B=[];text.split('\n').forEach(l=>{const m=l.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/);if(m)L.push(m[1]);else{if(L.length){B.push(<ul key={B.length}>{L.splice(0).map((x,i)=><li key={i}>{inl(x)}</li>)}</ul>)}if(l.trim())B.push(<p key={B.length}>{inl(l)}</p>)}});if(L.length)B.push(<ul key={B.length}>{L.map((x,i)=><li key={i}>{inl(x)}</li>)}</ul>);return B};
// Homepage-only Gaia Assistant. Answers come from GAIA's NISAR results via the backend, never invented.
export default function Assistant(){
 const[mons,setMons]=useState(undefined),[msgs,setMsgs]=useState([]),[q,setQ]=useState(''),[busy,setBusy]=useState(false);const log=useRef();const[ai,setAi]=useState(null);const hist=useRef([]);
 useEffect(()=>{loadMonitors().then(setMons);aiStatus().then(setAi)},[]);
 useEffect(()=>{const el=log.current;if(el)el.scrollTop=el.scrollHeight},[msgs,busy]);
 const offline=mons===null;
 // suggestions only for places and engines that actually have results
 const short=m=>{const n=(m.query&&!/^[-\d.\s,]+$/.test(m.query)?m.query:(m.name||'').split(',')[0]).trim();return n.charAt(0).toUpperCase()+n.slice(1)};
 const done=(mons||[]).filter(m=>(m.last_run_status==='done'||m.last_run_status==='partial')&&short(m)).sort((a,b)=>(b.engines||[]).length-(a.engines||[]).length);
 const Q={flood:p=>`Is ${p} flooding?`,deformation:p=>`Is the ground moving in ${p}?`,vegetation:p=>`When did the growing season start in ${p}?`};
 const sugg=[...new Set(done.flatMap(m=>(m.engines||[]).filter(e=>Q[e]).map(e=>Q[e](short(m)))))].slice(0,3).concat('What data is there for Kano?');
 const send=async text=>{text=text.trim();if(!text||busy)return;setQ('');setMsgs(m=>[...m,{me:true,text}]);setBusy(true);
  let a=ai?.enabled?await askAI(text,hist.current):null;if(a&&a.md)hist.current.push({role:'user',text},{role:'model',text:a.text});if(!a)a=await ask(text,mons||[]);setBusy(false);setMsgs(m=>[...m,a]);if(a.offline)setMons(null);else if(mons===null)loadMonitors().then(setMons)};
 return<div className="asst">
  <div className="asst-vis"><ParticleStory stage={3} count={8000} camZ={6.2} size={7}/><div className="asst-id"><span className="mono">Gaia Assistant</span><p>Reads the analyses GAIA has run on NISAR radar and answers with the numbers, confidence and sources.</p></div></div>
  <div className="asst-chat">
   <div className="asst-h"><span className="mono">Ask about a place</span><span className={'asst-st'+(offline?' off':mons?' on':'')}>{offline?'Backend offline':mons?`${ai?.enabled?'Gaia AI · ':''}${mons.length} monitored area${mons.length===1?'':'s'}`:'Connecting…'}</span></div>
   <div className="asst-log" ref={log} aria-live="polite">
    {!msgs.length&&<p className="asst-empty">Ask whether an area flooded, when its growing season started, or if the ground is moving. I answer only from GAIA’s results and say when there are none.{ai?.enabled?' Answers are written by Gemini and can be wrong — check figures in the dashboard.':''}</p>}
    <AnimatePresence initial={false}>{msgs.map((m,i)=><motion.div key={i} className={'msg'+(m.me?' me':'')} initial={{opacity:0,y:10}} animate={{opacity:1,y:0}} transition={{duration:.3}}>
     {m.md?<MD text={m.text}/>:<p>{m.text}</p>}
     {m.warn&&m.warn.length>0&&<small className="msg-w">⚑ {m.warn.join(' ')}</small>}
     {m.facts&&<dl>{m.facts.filter(f=>f[1]).map(f=><div key={f[0]}><dt>{f[0]}</dt><dd>{f[1]}</dd></div>)}</dl>}
     {m.src&&<small>Source: {m.src}</small>}
     {m.link&&<Link className="msg-l" to={m.link.href}>{m.link.label} →</Link>}
    </motion.div>)}</AnimatePresence>
    {busy&&<div className="msg typing" aria-label="Reading results"><i/><i/><i/></div>}
   </div>
   <div className="asst-s">{sugg.map(s=><button key={s} onClick={()=>send(s)} disabled={busy}>{s}</button>)}</div>
   <form className="asst-f" onSubmit={e=>{e.preventDefault();send(q)}}><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Ask about any place…" aria-label="Ask Gaia"/><button className="btn solid" disabled={busy||!q.trim()}>Ask</button></form>
  </div></div>}
