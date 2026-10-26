// Same browser-language aliases as the Logovo landing page; no IP geolocation.
export const aliases={be:'ru',uk:'ru',kk:'ru',hy:'ru',ka:'ru',tg:'ru'};
export function supportedLanguage(value){
  const base=String(value||'').trim().toLowerCase().split(/[-_]/)[0];
  return ['ru','en'].includes(base)?base:null;
}
export function chooseLanguage(saved,languages=[]){
  const explicit=supportedLanguage(saved);
  if(explicit)return explicit;
  for(const candidate of languages){
    const base=String(candidate).toLowerCase().split(/[-_]/)[0];
    const match=supportedLanguage(aliases[base]||base);
    if(match)return match;
  }
  return 'en';
}
