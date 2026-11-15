import {useLocation} from 'react-router-dom';
// The dashboard (public/gaia-app.html) in a full-page frame. ?q=<place> and ?monitor=<id> are passed through.
export default function Dashboard(){const{search}=useLocation();return<iframe title="Gaia dashboard" src={`/gaia-app.html${search}#dashboard`} allow="fullscreen" allowFullScreen style={{position:'fixed',inset:0,width:'100%',height:'100%',border:0,background:'#121212'}}/>}
