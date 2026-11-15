import {useRef} from 'react';import {Link} from 'react-router-dom';import {motion,useScroll,useSpring,useTransform} from 'framer-motion';
import Particles from './Particles';import {Words} from '../lib/grid';
// North heroHome: .heroTop (absolute, 100svh) + .heroHome-timeline (sticky) driven by scroll progress
export default function Hero(){const ref=useRef();const{scrollYProgress:p}=useScroll({target:ref,offset:['start start','end end']});const sp=useSpring(p,{stiffness:80,damping:22,mass:.5});
 const topO=useTransform(sp,[0,.18],[1,0]),topY=useTransform(sp,[0,.18],[0,-70]),introO=useTransform(sp,[.22,.34,.62,.72],[0,1,1,0]),introY=useTransform(sp,[.22,.34,.62,.72],[40,0,0,-40]),endO=useTransform(sp,[.74,.86],[0,1]),morph=useTransform(sp,[.1,.85],[0,1]);
 return<section ref={ref} className="hero" data-bg="black"><div className="hero-tl"><div className="hero-sticky">
  <Particles progress={morph} className="hero-gl"/>
  <motion.div className="heroTop px" style={{opacity:topO,y:topY}}><div className="g hero-grid"><div className="fl"/><div className="fr"/>
   <div className="c34 hero-head"><Words as="h1" className="t1 hero-title" text="Know the ground before it moves."/></div>
   <div className="c34 hero-block"><div className="hero-btns c17"><Link className="btn solid" to="/dashboard">Open dashboard</Link><Link className="btn" to="/request-demo">Request demo</Link></div>
    <p className="hero-desc c8">Gaia reads NASA satellite and radar data for any place on Earth: land deformation, planting seasons and flood exposure in one calm dashboard.</p></div></div></motion.div>
  <motion.div className="hero-intro" style={{opacity:introO,y:introY}}><p className="sub">Earth observation</p><p className="t1">Dancing with the SARs</p><p className="sub">needed a clear reading.</p></motion.div>
  <motion.div className="hero-end" style={{opacity:endO}}><span className="mono">Scroll</span><Link className="btn solid" to="/dashboard">Search a place</Link></motion.div>
 </div></div></section>}
