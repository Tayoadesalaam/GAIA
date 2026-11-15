import {motion} from 'framer-motion';
const ease=[.16,1,.3,1];
// 34-column grid cell with dotted borders and corner crosses (North "container-content")
export const Cell=({span=34,children,className='',nb=''})=><div className={`cell c${span} ${nb} ${className}`}>{children}</div>;
export const Sec=({bg='black',className='',children})=><section data-bg={bg} className={'sec '+className}><div className="px"><div className="g">{children}</div></div></section>;
export const Reveal=({children,d=0,className='',y=32})=><motion.div className={className} initial={{opacity:0,y}} whileInView={{opacity:1,y:0}} viewport={{once:true,margin:'-8% 0px'}} transition={{duration:.9,delay:d,ease}}>{children}</motion.div>;
// word-by-word masked line reveal for headings. The heading itself is observed: the words start fully
// clipped inside their masks, so observing them directly would never fire and leave the heading hidden.
const wv={h:{y:'110%'},v:i=>({y:0,transition:{duration:.9,delay:i*.04,ease}})};
export const Words=({text,as:T='h2',className='t1'})=>{const M=motion[T]||motion.h2;return<M className={className} aria-label={text} initial="h" whileInView="v" viewport={{once:true,amount:0}}>{text.split(' ').map((w,i)=><span key={i} className="mask" aria-hidden><motion.span custom={i} variants={wv}>{w}&nbsp;</motion.span></span>)}</M>};
// Genuine dashboard screenshots live in public/shots/<name>.jpg; without one the slot shows its label.
export const ImgSlot=({label,shot,ratio='4/3',className=''})=>shot?<figure className={'shot '+className} style={{aspectRatio:ratio}}><img src={`/shots/${shot}.jpg`} alt={label} loading="lazy" decoding="async"/></figure>:<div className={'slot '+className} style={{aspectRatio:ratio}}><span>{label}</span></div>;
