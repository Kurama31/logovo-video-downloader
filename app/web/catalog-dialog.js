import {setText} from './i18n.js';
import {api} from './downloader.js';
import {reportError,errorText} from './error-dialog.js';

const dialog=document.querySelector('#catalog-dialog');
const form=document.querySelector('#catalog-form');
const voice=document.querySelector('#catalog-voice');
const episodes=document.querySelector('#catalog-episodes');
const all=document.querySelector('#catalog-all');
const error=document.querySelector('#catalog-error');
let current=null,selectHandler=null,submitting=false;

function group(){return current?.catalog.groups.find(item=>item.id===voice.value);}
export function formatEpisodeRanges(numbers){
  const values=[...new Set(numbers.map(String))];
  const numeric=values.filter(n=>/^\d+$/.test(n)).map(Number).sort((a,b)=>a-b);
  const result=[];
  for(let i=0;i<numeric.length;i++){
    const first=numeric[i];let last=first;
    while(i+1<numeric.length&&numeric[i+1]===last+1)last=numeric[++i];
    result.push(first===last?String(first):`с ${first} по ${last}`);
  }
  result.push(...values.filter(n=>!/^\d+$/.test(n)));
  return result.join(', ');
}
function selectionSummary(){
  const summary=document.querySelector('#catalog-selection');
  const numbers=episodes.value.split(',').map(n=>n.trim()).filter(Boolean);
  summary.hidden=all.checked||!numbers.length;
  setText(summary,numbers.length?'Выбрано: '+formatEpisodeRanges(numbers):'');
}
function update(){
  const value=group();
  episodes.disabled=false;episodes.readOnly=all.checked;
  episodes.classList.toggle('is-inactive',all.checked);
  setText(document.querySelector('#catalog-available'),current?.catalog.kind==='embedded'?
    (current.catalog.items||[]).map(item=>`${item.number}: ${item.title}`).join('\n'):
    value?`Доступные серии: ${formatEpisodeRanges(value.available)||'нет'}. Всего в озвучке: ${value.episodes.length}.`:'Нет поддерживаемых серий.');
  document.querySelector('#catalog-submit').disabled=submitting||!value?.available.length;
  setText(error,'');
  selectionSummary();
}

export function showCatalog(job,handler){
  if(dialog.open)return;
  current=job;selectHandler=handler;
  const embedded=job.catalog.kind==='embedded';
  setText(document.querySelector('#catalog-intro'),embedded?'На странице найдены возможные видеоисточники. Выберите нужный вариант.':'По ссылке найдено несколько видео. Выберите озвучку, формат и серии.');
  setText(document.querySelector('#catalog-voice-label'),embedded?'Источники':'Озвучка');
  setText(document.querySelector('#catalog-all-label'),embedded?'Все найденные варианты':'Все доступные серии выбранной озвучки');
  setText(document.querySelector('#catalog-numbers-label'),embedded?'Номера вариантов через запятую':'Или номера через запятую');
  setText(document.querySelector('#catalog-submit'),embedded?'Скачать выбранное':'Скачать выбранные серии');
  setText(document.querySelector('#catalog-title'),job.catalog.title,true);
  setText(document.querySelector('#catalog-note'),job.catalog.note);
  voice.replaceChildren();
  const groups=[...job.catalog.groups].sort((a,b)=>Number(!!b.available.length)-Number(!!a.available.length));
  for(const item of groups){
    const option=document.createElement('option');option.value=item.id;
    setText(option,item.label+(item.available.length?` · ${item.available.length} ${embedded?'вариантов':'серий'}`:' · плеер пока не поддерживается'));
    option.disabled=!item.available.length;voice.append(option);
  }
  voice.value=groups.find(item=>item.available.length)?.id||'';
  all.checked=!embedded;episodes.value=embedded?'1':'';document.querySelector('#catalog-container').value='source';
  update();dialog.showModal();
}
export function closeCatalog(){if(dialog.open&&!submitting)dialog.close();current=null;}
voice.onchange=update;
all.onchange=()=>{if(all.checked)episodes.value='';update();};
function manualEntry(){
  if(submitting)return;
  if(all.checked){all.checked=false;update();}
}
episodes.addEventListener('focus',manualEntry);
episodes.addEventListener('pointerdown',manualEntry);
episodes.addEventListener('input',selectionSummary);
document.querySelector('#catalog-numbers-label').addEventListener('click',manualEntry);
document.querySelector('#catalog-close').onclick=()=>dialog.close();
dialog.addEventListener('cancel',event=>{if(submitting)event.preventDefault();});
form.onsubmit=async event=>{
  event.preventDefault();if(submitting||!current)return;
  const numbers=all.checked?null:[...new Set(episodes.value.split(',').map(n=>n.trim()).filter(Boolean))];
  if(numbers&&(!numbers.length||numbers.some(n=>!group().available.includes(n)))){
    setText(error,'Введите доступные номера через запятую, например: 1, 3, 5.');return;
  }
  submitting=true;update();document.querySelector('#catalog-close').disabled=true;
  try{
    const result=await api(`catalogs/${current.id}/selection`,'POST',{
      voice_id:voice.value,episodes:numbers,container:document.querySelector('#catalog-container').value});
    if(!Array.isArray(result.jobs)||!result.jobs.length)throw new Error('Сервер не создал задания для выбранных серий. Повторите выбор.');
    dialog.close();Promise.resolve(selectHandler(result.jobs)).catch(e=>reportError(e,{title:'Не удалось запустить очередь'}));
  }catch(e){setText(error,errorText(e));reportError(e,{title:'Не удалось добавить выбранные видео'});}
  finally{submitting=false;document.querySelector('#catalog-close').disabled=false;document.querySelector('#catalog-submit').disabled=!group()?.available.length;}
};
