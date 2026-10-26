import {english} from './translations-en.js';

const patterns=Object.entries(english).filter(([key])=>/\{\w+\}/.test(key)).sort(([a],[b])=>b.replace(/\{\w+\}/g,'').length-a.replace(/\{\w+\}/g,'').length).map(([source,target])=>{
  const names=[];
  const parts=source.split(/(\{\w+\})/).map(part=>{
    if(/^\{\w+\}$/.test(part)){
      const name=part.slice(1,-1);names.push(name);
      return ['value','percent','done','total','remaining','errors','cancelled','count','number','first','last'].includes(name)?'([0-9]+(?:[.,][0-9]+)?)':'([\\s\\S]+?)';
    }
    return part.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  });
  return {regex:new RegExp('^'+parts.join('')+'$'),names,target,source};
});
export function translateMessage(value,language='ru'){
  if(value==null)return '';
  const source=String(value);
  if(language==='ru'||!source)return source;
  const translate=value=>translateMessage(value,language);
  const trimmed=source.trim(),prefix=source.slice(0,source.length-source.trimStart().length),suffix=source.slice(source.trimEnd().length);
  if(Object.hasOwn(english,trimmed))return prefix+english[trimmed]+suffix;
  if(source.includes('\n'))return source.split('\n').map(translate).join('\n');
  for(const {regex,names,target,source:patternSource} of patterns){
    if(['Плеер: {name}','Библиотека: {name}','Поток: {name}'].includes(patternSource)&&trimmed.includes(' · '))continue;
    const match=trimmed.match(regex);
    if(match){
      const values=Object.fromEntries(names.map((name,index)=>[name,match[index+1]]));
      return prefix+target.replace(/\{(\w+)\}/g,(_,name)=>name==='numbers'?values[name].split(', ').map(translate).join(', '):['error','detail','stage','note'].includes(name)?translate(values[name]):values[name])+suffix;
    }
  }
  for(const separator of [' · ',' / '])if(source.includes(separator))return source.split(separator).map(translate).join(separator);
  if(/\/с$/.test(source))return translate(source.slice(0,-2))+'/s';
  return source;
}
