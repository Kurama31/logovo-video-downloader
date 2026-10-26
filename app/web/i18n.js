import {translateMessage} from './translation-core.js';
import {chooseLanguage,supportedLanguage} from './language-policy.js';

const cookieName='dav_language';
const bindings=new Map();
let pruneScheduled=false;
let manual=null;
try{manual=supportedLanguage(decodeURIComponent(document.cookie.split('; ').find(part=>part.startsWith(cookieName+'='))?.slice(cookieName.length+1)||''));}catch{}
let language=chooseLanguage(manual,navigator.languages?.length?navigator.languages:[navigator.language]);
export const getLanguage=()=>language;
export const getLocale=()=>language==='ru'?'ru-RU':'en-GB';
export const translate=value=>translateMessage(value,language);
function remember(node,kind,source,raw=false){
  let entries=bindings.get(node);
  if(!entries){entries=new Map();bindings.set(node,entries);}
  entries.set(kind,{source,raw});
  if(!pruneScheduled){
    pruneScheduled=true;requestAnimationFrame(()=>{
      pruneScheduled=false;
      const templates=[...document.querySelectorAll('template')];
      for(const bound of bindings.keys())if(!bound.isConnected&&!templates.some(template=>template.content.contains(bound)))bindings.delete(bound);
    });
  }
}
export function setText(node,value,raw=false){
  const source=value==null?'':String(value);
  remember(node,'text',source,raw);
  node.textContent=raw?source:translate(source);
}
export function setAttributeText(node,name,value){
  remember(node,name,String(value));node.setAttribute(name,translate(value));
}
export function translateStatic(root=document){
  for(const node of root.querySelectorAll('[data-i18n]'))node.textContent=translate(node.dataset.i18n);
  for(const name of ['aria-label','title','placeholder'])for(const node of root.querySelectorAll('[data-i18n-'+name+']'))node.setAttribute(name,translate(node.getAttribute('data-i18n-'+name)));
  for(const template of root.querySelectorAll('template'))translateStatic(template.content);
}
export function setLanguage(value){
  manual=['ru','en'].includes(value)?value:null;
  language=chooseLanguage(manual,navigator.languages?.length?navigator.languages:[navigator.language]);
  try{document.cookie=`${cookieName}=${manual||''}; Max-Age=${manual?34560000:0}; Path=/; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;}catch{}
  applyLanguage();
}
function applyLanguage(){
  document.documentElement.lang=language;
  translateStatic();
  for(const [node,entries] of bindings)for(const [kind,{source,raw}] of entries){
    if(kind==='text')node.textContent=raw?source:translate(source);
    else node.setAttribute(kind,translate(source));
  }
  for(const selector of document.querySelectorAll('[data-language-select]'))selector.value=manual||'auto';
  window.dispatchEvent(new CustomEvent('dav-language-change',{detail:{language,automatic:!manual}}));
}
for(const selector of document.querySelectorAll('[data-language-select]'))selector.addEventListener('change',event=>setLanguage(event.target.value));
window.addEventListener('languagechange',()=>{if(!manual){language=chooseLanguage(null,navigator.languages);applyLanguage();}});
applyLanguage();
