const NAME='dav_consent',VERSION=1,MAX_AGE=180*86400;
let initialized=false,resolveInitial;
export function consentChoice(){
  try{
    const raw=document.cookie.split('; ').find(part=>part.startsWith(NAME+'='))?.slice(NAME.length+1);
    const value=JSON.parse(decodeURIComponent(raw||''));
    return value.version===VERSION&&typeof value.preferences==='boolean'&&
      Number.isFinite(value.updatedAt)&&value.updatedAt<=Date.now()+60000&&
      Date.now()-value.updatedAt<MAX_AGE*1000?value:null;
  }catch{return null;}
}
export const preferencesAllowed=()=>consentChoice()?.preferences===true;
export function removePreferenceCookie(name){document.cookie=`${name}=; Max-Age=0; Path=/; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;}
export async function initConsent(){
  const dialog=document.querySelector('#consent-dialog'),settings=document.querySelector('#consent-settings');
  const toggle=document.querySelector('#consent-preferences');
  if(!initialized){
    initialized=true;
    const open=()=>{
      toggle.checked=preferencesAllowed();settings.hidden=true;
      document.querySelector('#consent-save').hidden=true;
      document.querySelector('#consent-customize').setAttribute('aria-expanded','false');
      if(!dialog.open)dialog.showModal();
    };
    const save=preferences=>{
      const choice={version:VERSION,preferences,updatedAt:Date.now()};
      try{document.cookie=`${NAME}=${encodeURIComponent(JSON.stringify(choice))}; Max-Age=${MAX_AGE}; Path=/; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;}catch{}
      if(!preferences){removePreferenceCookie('dav_quality');removePreferenceCookie('dav_destination');}
      window.dispatchEvent(new CustomEvent('dav-consent-change',{detail:choice}));
      dialog.close();resolveInitial?.();resolveInitial=null;
    };
    document.querySelector('#consent-accept').onclick=()=>save(true);
    document.querySelector('#consent-necessary').onclick=()=>save(false);
    document.querySelector('#consent-save').onclick=()=>save(toggle.checked);
    document.querySelector('#consent-customize').onclick=()=>{
      settings.hidden=!settings.hidden;document.querySelector('#consent-save').hidden=settings.hidden;
      document.querySelector('#consent-customize').setAttribute('aria-expanded',String(!settings.hidden));
    };
    document.querySelector('#privacy-settings').onclick=open;
    dialog.addEventListener('cancel',event=>{if(resolveInitial)event.preventDefault();});
    if(!consentChoice()){
      await new Promise(resolve=>{resolveInitial=resolve;open();});
    }
  }
  if(!preferencesAllowed()){removePreferenceCookie('dav_quality');removePreferenceCookie('dav_destination');}
}
